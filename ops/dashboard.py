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


# Tranches de confiance, choisies sur l'usage métier plutôt que par découpage
# régulier : ce sont les quatre décisions qu'un juriste peut prendre devant un
# score. Un histogramme en dixièmes serait plus « neutre » et moins actionnable.
TRANCHES_CONFIANCE: tuple[tuple[str, float, float], ...] = (
    ("moins de 0,50 - a refaire", 0.0, 0.50),
    ("0,50 a 0,70 - a relire", 0.50, 0.70),
    ("0,70 a 0,85 - a verifier", 0.70, 0.85),
    ("0,85 et plus - exploitable", 0.85, 1.0001),
)


def distribuer(scores: list[float]) -> dict[str, int]:
    """Répartit les scores dans les tranches métier.

    La moyenne seule est trompeuse sur une distribution bimodale : dix analyses
    à 0,95 et dix à 0,45 donnent la même moyenne que vingt analyses à 0,70 — mais
    dans le premier cas la moitié du travail est à refaire, dans le second aucun
    n'est franchement mauvais. C'est la raison pour laquelle le brief demande la
    *distribution* du score, et c'est elle qui doit servir de référence de dérive.
    """
    compte = {libelle: 0 for libelle, _, _ in TRANCHES_CONFIANCE}
    for score in scores:
        for libelle, bas, haut in TRANCHES_CONFIANCE:
            if bas <= score < haut:
                compte[libelle] += 1
                break
    return compte


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
        "score_p10": round(percentile(scores, 0.10), 4) if scores else None,
        "score_p50": round(percentile(scores, 0.50), 4) if scores else None,
        "score_p90": round(percentile(scores, 0.90), 4) if scores else None,
        "distribution_score": distribuer(scores),
        "scores_mesures": len(scores),
        "cout_total_eur": round(sum(m.cout_eur for m in mesures), 6),
        "cout_moyen_eur": round(statistics.fmean([m.cout_eur for m in mesures]), 6)
        if mesures
        else 0.0,
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
    for version, v in r["par_version"].items():
        if not v["scores_mesures"]:
            continue
        lignes += ["", f"distribution du score de confiance — {version}"
                       f"  (P10 {v['score_p10']:.2f} · P50 {v['score_p50']:.2f}"
                       f" · P90 {v['score_p90']:.2f})"]
        total = max(1, v["scores_mesures"])
        for libelle, compte in v["distribution_score"].items():
            barre = "#" * round(compte / total * 30)
            lignes.append(f"  {libelle:<24} {compte:>4} {barre}")
    if r.get("journal"):
        lignes += ["", "journal de pilotage (5 derniers) :"]
        for e in r["journal"]:
            detail = e.get("motif") or e.get("signal") or e.get("version") or ""
            lignes.append(f"  {e.get('date', '')}  {e['evenement']:<28} {detail}")
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
    distributions = ""
    for version, v in r["par_version"].items():
        if not v["scores_mesures"]:
            continue
        total = max(1, v["scores_mesures"])
        barres = "".join(
            f"<div class=tranche><span class=lbl>{libelle}</span>"
            f"<span class=jauge><i style='width:{compte / total * 100:.1f}%'></i></span>"
            f"<span class=cnt>{compte}</span></div>"
            for libelle, compte in v["distribution_score"].items()
        )
        distributions += (
            f"<h3>{version} — distribution du score "
            f"<small>P10 {v['score_p10']:.2f} · P50 {v['score_p50']:.2f} ·"
            f" P90 {v['score_p90']:.2f}</small></h3>{barres}"
        )
    journal = "".join(
        f"<li><b>{e['evenement']}</b> — {e.get('date', '')} "
        f"{e.get('motif') or e.get('signal') or e.get('version') or ''}</li>"
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
 h3{{margin:1.2rem 0 .4rem;font-size:.95rem}} h3 small{{font-weight:400;color:#666}}
 .tranche{{display:flex;align-items:center;gap:.6rem;max-width:40rem;margin:.15rem 0}}
 .lbl{{width:13rem;font-size:.85rem;color:#444}}
 .jauge{{flex:1;background:#eee;height:.8rem;border-radius:.4rem;overflow:hidden}}
 .jauge i{{display:block;height:100%;background:#2b6cb0}}
 .cnt{{width:2.5rem;text-align:right;font-variant-numeric:tabular-nums}}
</style></head><body>
<h1>Mardik — pilotage de version</h1>
<p class=etat>fenêtre {int(r['fenetre_s'])} s · {r['total']} requête(s) ·
 actif <b>{index.get('active') or '—'}</b> ·
 canary <b>{index.get('canary') or '—'}</b> ({index.get('canary_percent', 0)} %)</p>
<table><thead><tr><th>version</th><th>trafic</th><th>req.</th><th>P50 ms</th><th>P95 ms</th>
<th>erreurs</th><th>confiance</th><th>coût €</th></tr></thead><tbody>{corps}</tbody></table>
<h2>Distribution du score de confiance</h2>
{distributions or '<p>aucun score dans la fenêtre</p>'}
<h2>Journal de pilotage</h2><ul>{journal or '<li>aucun</li>'}</ul>
</body></html>"""


def _console_tolerante() -> None:
    """Une console Windows en cp1252 ne doit pas faire planter un outil d'astreinte."""
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _console_tolerante()
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
