"""Tests d'acceptance — pilotage et observabilité (5 tests, tous rouges au départ).

Chaque docstring reprend la phrase du brief : Étant donné / quand / alors.
"""
from __future__ import annotations

import time

from app.llm_client import Bundle
from app.telemetry import Mesure


def _livrer_v2(registry, version="v2.0.0"):
    registry.etiqueter(version, Bundle.charger("v2"), commit="abc1234", note_eval=0.9)
    return version


def test_rollback_en_une_operation(client, registry):
    """Étant donné une v2 promue en production après la v1, quand on déclenche un
    rollback, alors la v1 redevient la version active pour 100 % du trafic, sans
    redémarrage, et le client v1 continue de fonctionner."""
    from ops.deploy import promouvoir, rollback

    _livrer_v2(registry)
    promouvoir("v2.0.0", registry=registry)
    assert client.get("/gateway/etat").json()["active"] == "v2.0.0"

    index = rollback(registry=registry, motif="test")
    assert index["active"] == "v1.0.0"
    assert index["canary"] is None
    etat = client.get("/gateway/etat").json()
    assert etat["active"] == "v1.0.0" and etat["canary"] is None
    assert client.post("/v1/analyse", json={"texte": "x" * 40}).status_code == 200


def test_promotion_canary_puis_totale(client, registry, contrat):
    """Étant donné une v2 étiquetée, quand on la déploie en canary à 30 % puis qu'on la
    promeut, alors la gateway sert d'abord un mélange v1/v2 (en-tête X-Mardik-Version),
    puis 100 % v2 après promotion ; chaque étape est journalisée."""
    from app.gateway import choisir_version
    from ops.deploy import deployer_canary, promouvoir

    _livrer_v2(registry)
    index = deployer_canary("v2.0.0", pourcentage=30, registry=registry)
    assert index["canary"] == "v2.0.0" and index["canary_percent"] == 30
    assert index["active"] == "v1.0.0"

    # la fonction de routage est pure et respecte le pourcentage
    tirages = [choisir_version("v1.0.0", "v2.0.0", 30, t) for t in range(100)]
    assert tirages.count("v2.0.0") == 30

    texte = contrat("c02")
    versions = {
        client.post("/analyse", json={"texte": texte}).headers["x-mardik-version"]
        for _ in range(40)
    }
    assert versions == {"v1.0.0", "v2.0.0"}, "le canary doit recevoir une partie du trafic"

    index = promouvoir("v2.0.0", registry=registry)
    assert index["active"] == "v2.0.0" and index["canary"] is None
    r = client.post("/analyse", json={"texte": texte})
    assert r.headers["x-mardik-version"] == "v2.0.0"
    assert "confiance_globale" in r.json()
    evenements = [e["evenement"] for e in registry.journal()]
    assert evenements[-2:] == ["canary", "promotion"]


def test_evaluation_enrichie_latence_et_cout(historique):
    """Étant donné les contraintes du client (latence P95 < 8 s, coût < 0,15 € par
    analyse), quand le gate d'évaluation s'exécute, alors le rapport contient, en plus
    de la note, la latence P95 et le coût moyen par analyse, et le gate échoue si une
    contrainte n'est pas respectée."""
    from eval.run_eval import evaluer

    rapport = evaluer("v2", sous_ensemble=["c01", "c02", "c03"], historique=historique)
    assert rapport.latence_p95_ms >= 0
    assert rapport.cout_moyen_eur >= 0
    assert rapport.passe and rapport.motifs == []

    serre = evaluer(
        "v2",
        sous_ensemble=["c01", "c02", "c03"],
        historique=historique,
        latence_max_ms=0.0001,
        cout_max_eur=0.0,
    )
    assert not serre.passe
    assert any("latence" in m for m in serre.motifs)
    assert any("coût" in m for m in serre.motifs)
    d = rapport.to_dict()
    assert {"version", "note", "latence_p95_ms", "cout_moyen_eur", "passe", "par_contrat"} <= set(d)


def test_dashboard_par_version(metriques, registry):
    """Étant donné du trafic servi par la v1 et la v2, quand on consulte le tableau de
    bord, alors il présente, par version : le trafic, la latence (P50/P95), le taux
    d'erreur et le score de confiance moyen."""
    from ops.dashboard import rendre_texte, resume

    maintenant = time.time()
    for i in range(20):
        metriques.enregistrer(
            Mesure(ts=maintenant, version="v1.0.0", route="/analyse", latence_ms=800 + i * 10,
                   erreur=(i == 0), score=None, cout_eur=0.001)
        )
    for i in range(10):
        metriques.enregistrer(
            Mesure(ts=maintenant, version="v2.0.0", route="/analyse", latence_ms=3000 + i * 100,
                   erreur=False, score=0.8 + i * 0.01, cout_eur=0.02)
        )
    r = resume(metriques, fenetre_s=60, registry=registry)
    assert r["total"] == 30
    v1, v2 = r["par_version"]["v1.0.0"], r["par_version"]["v2.0.0"]
    assert {"requetes", "trafic_pct", "latence_p50_ms", "latence_p95_ms", "taux_erreur", "score_moyen"} <= set(v1)
    assert v1["requetes"] == 20 and v2["requetes"] == 10
    assert abs(v1["trafic_pct"] - 66.7) < 0.1
    assert v1["taux_erreur"] == 0.05 and v2["taux_erreur"] == 0.0
    assert v1["score_moyen"] is None
    assert 0.84 <= v2["score_moyen"] <= 0.85
    assert v2["latence_p95_ms"] >= v2["latence_p50_ms"] >= 3000
    assert "v2.0.0" in rendre_texte(r)


def test_journal_derive_et_rollback_automatique(metriques, registry):
    """Étant donné un canary v2 dont le score de confiance dérive en production, quand
    la surveillance s'exécute, alors elle détecte la dérive, déclenche le rollback et
    inscrit au journal une entrée datée avec le motif et les versions avant/après."""
    from ops.deploy import deployer_canary, surveiller

    _livrer_v2(registry)
    deployer_canary("v2.0.0", pourcentage=20, registry=registry)

    # trafic sain : pas de dérive
    maintenant = time.time()
    for i in range(12):
        metriques.enregistrer(
            Mesure(ts=maintenant, version="v2.0.0", route="/analyse", latence_ms=2500, score=0.88)
        )
    res = surveiller(registry, metriques, fenetre_s=60, score_min=0.7, minimum=10)
    assert res["derive"] is False and res["rollback"] is False
    assert registry.canary()[0] == "v2.0.0"

    # dérive : les scores tombent vers 0,5 (ce que fait le proxy en mode DRIFT=score)
    for i in range(15):
        metriques.enregistrer(
            Mesure(ts=time.time(), version="v2.0.0", route="/analyse", latence_ms=2500, score=0.5)
        )
    res = surveiller(registry, metriques, fenetre_s=60, score_min=0.7, minimum=10)
    assert res["derive"] is True and res["rollback"] is True
    assert "score" in res["motif"]
    assert registry.canary() == (None, 0)
    assert registry.active() == "v1.0.0"

    entree = registry.journal()[-1]
    assert entree["evenement"] == "rollback"
    assert entree["date"] and "score" in entree["motif"]
    assert entree["avant"]["canary"] == "v2.0.0" and entree["apres"]["canary"] is None
