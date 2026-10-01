"""Tableau de bord : latence, erreurs, score de confiance, trafic par version.

    resume(metriques=None, *, fenetre_s=300, registry=None) -> dict
        Agrège les mesures des ``fenetre_s`` dernières secondes par version :
        requêtes, part de trafic, latence P50/P95, taux d'erreur, score de
        confiance moyen, coût — plus les 5 derniers événements de déploiement.

    python -m ops.dashboard              → affiche le résumé en texte
    python -m ops.dashboard --serve      → page HTML auto-rafraîchie sur :8501

``agreger()`` est exporté et utilisé tel quel par ``ops.deploy.surveiller`` :
l'alerte qui déclenche un rollback et le tableau de bord qu'on regarde pour
comprendre ce rollback calculent leurs chiffres avec le **même** code. Deux
implémentations parallèles finiraient par diverger — typiquement sur le détail
qui compte, comme savoir si les requêtes en erreur entrent dans la latence
moyenne — et on passerait l'incident à se demander lequel des deux ment.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from typing import Any

from app.telemetry import Mesure, MetricsStore
from ops.registry import Registry


def percentile(valeurs: list[float], q: float) -> float:
    """Percentile par rang le plus proche — pas d'interpolation, donc toujours
    une valeur réellement observée : plus facile à retrouver dans les traces."""
    if not valeurs:
        return 0.0
    tri = sorted(valeurs)
    rang = max(1, min(len(tri), math.ceil(q * len(tri))))
    return tri[rang - 1]


def agreger(mesures: list[Mesure]) -> dict[str, Any]:
    """Les indicateurs d'un lot de mesures (une version, une fenêtre).

    Les requêtes en erreur comptent dans le taux d'erreur mais sont exclues des
    latences et des scores : une requête qui a échoué en 20 ms ferait baisser la
    latence P95 au moment même où le service se dégrade.
    """
    saines = [m for m in mesures if not m.erreur]
    latences = [m.latence_ms for m in saines]
    scores = [m.score for m in saines if m.score is not None]
    erreurs = sum(1 for m in mesures if m.erreur)
    return {
        "requetes": len(mesures),
        "erreurs": erreurs,
        "taux_erreur": round(erreurs / len(mesures), 4) if mesures else 0.0,
        "latence_p50_ms": round(percentile(latences, 0.50), 1),
        "latence_p95_ms": round(percentile(latences, 0.95), 1),
        "score_moyen": round(statistics.fmean(scores), 4) if scores else None,
        "cout_total_eur": round(sum(m.cout_eur for m in mesures), 6),
        "appels_llm": sum(m.appels_llm for m in mesures),
        "tronques": sum(1 for m in mesures if m.tronque),
    }


def resume(
    metriques: MetricsStore | None = None,
    *,
    fenetre_s: float = 300,
    registry: Registry | None = None,
) -> dict[str, Any]:
    """Le tableau de bord : une ligne par version ayant servi du trafic."""
    store = metriques or MetricsStore()
    mesures = store.lire(depuis_s=fenetre_s)

    par_version: dict[str, dict[str, Any]] = {}
    for version in sorted({m.version for m in mesures}):
        lot = [m for m in mesures if m.version == version]
        indicateurs = agreger(lot)
        indicateurs["trafic_pct"] = round(len(lot) / len(mesures) * 100, 1)
        par_version[version] = indicateurs

    registre = registry or _registre_silencieux()
    return {
        "fenetre_s": fenetre_s,
        "total": len(mesures),
        "par_version": par_version,
        "index": registre.index() if registre else {},
        "journal": registre.journal()[-5:] if registre else [],
    }


def _registre_silencieux() -> Registry | None:
    """Le tableau de bord doit s'afficher même sans registre lisible."""
    try:
        return Registry()
    except OSError:
        return None


def rendre_texte(r: dict[str, Any]) -> str:
    index = r.get("index") or {}
    lignes = [
        f"== Mardik — {r['total']} requête(s) sur {int(r['fenetre_s'])} s ==",
        f"actif : {index.get('active') or '—'}"
        f"   canary : {index.get('canary') or '—'} ({index.get('canary_percent', 0)} %)",
        "",
        f"{'version':<10} {'trafic':>8} {'req':>5} {'P50 ms':>9} {'P95 ms':>9}"
        f" {'err':>7} {'confiance':>10} {'coût €':>9}",
    ]
    if not r["par_version"]:
        lignes.append("  (aucun trafic dans la fenêtre)")
    for version, v in r["par_version"].items():
        score = "—" if v["score_moyen"] is None else f"{v['score_moyen']:.3f}"
        lignes.append(
            f"{version:<10} {v['trafic_pct']:>7.1f}% {v['requetes']:>5}"
            f" {v['latence_p50_ms']:>9.0f} {v['latence_p95_ms']:>9.0f}"
            f" {v['taux_erreur']:>6.1%} {score:>10} {v['cout_total_eur']:>9.4f}"
        )
    if r.get("journal"):
        lignes += ["", "derniers événements de déploiement :"]
        for e in r["journal"]:
            detail = e.get("motif") or e.get("version") or ""
            lignes.append(f"  {e.get('date', '')}  {e['evenement']:<12} {detail}")
    return "\n".join(lignes)


def rendre_html(r: dict[str, Any]) -> str:
    index = r.get("index") or {}
    corps = "".join(
        f"<tr><td class=v>{version}</td><td>{v['trafic_pct']:.1f} %</td><td>{v['requetes']}</td>"
        f"<td>{v['latence_p50_ms']:.0f}</td><td>{v['latence_p95_ms']:.0f}</td>"
        f"<td class='{'ko' if v['taux_erreur'] > 0.1 else ''}'>{v['taux_erreur']:.1%}</td>"
        f"<td class='{'ko' if (v['score_moyen'] or 1) < 0.7 else ''}'>"
        f"{'—' if v['score_moyen'] is None else format(v['score_moyen'], '.3f')}</td>"
        f"<td>{v['cout_total_eur']:.4f}</td></tr>"
        for version, v in r["par_version"].items()
    ) or "<tr><td colspan=8>aucun trafic dans la fenêtre</td></tr>"
    journal = "".join(
        f"<li><b>{e['evenement']}</b> — {e.get('date', '')} "
        f"{e.get('motif') or e.get('version') or ''}</li>"
        for e in r.get("journal", [])
    )
    return f"""<!doctype html><html lang=fr><head><meta charset=utf-8>
<meta http-equiv=refresh content=5><title>Mardik — pilotage</title>
<style>
 body{{font:14px/1.5 system-ui,sans-serif;margin:2rem;color:#111;background:#fafafa}}
 table{{border-collapse:collapse;width:100%;max-width:60rem;background:#fff}}
 th,td{{padding:.45rem .7rem;border-bottom:1px solid #e5e5e5;text-align:right}}
 th:first-child,td:first-child{{text-align:left}}
 th{{background:#f0f0f0;font-weight:600}}
 .v{{font-weight:600}} .ko{{color:#b00020;font-weight:700}}
 .etat{{margin:.5rem 0 1.5rem;color:#555}}
</style></head><body>
<h1>Mardik — pilotage de version</h1>
<p class=etat>fenêtre {int(r['fenetre_s'])} s · {r['total']} requête(s) ·
 actif <b>{index.get('active') or '—'}</b> ·
 canary <b>{index.get('canary') or '—'}</b> ({index.get('canary_percent', 0)} %)</p>
<table><thead><tr><th>version</th><th>trafic</th><th>req.</th><th>P50 ms</th><th>P95 ms</th>
<th>erreurs</th><th>confiance</th><th>coût €</th></tr></thead><tbody>{corps}</tbody></table>
<h2>Derniers événements</h2><ul>{journal or '<li>aucun</li>'}</ul>
</body></html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tableau de bord Mardik")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--fenetre", type=float, default=300)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.serve:
        import uvicorn
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse

        app = FastAPI(title="Mardik dashboard")

        @app.get("/", response_class=HTMLResponse)
        def page() -> str:
            return rendre_html(resume(fenetre_s=args.fenetre))

        @app.get("/api")
        def api() -> dict[str, Any]:
            return resume(fenetre_s=args.fenetre)

        uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")
        return 0
    r = resume(fenetre_s=args.fenetre)
    print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else rendre_texte(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
