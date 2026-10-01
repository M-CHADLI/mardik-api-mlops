"""Tests d'acceptance — les critères du brief non couverts par les 10 tests fournis.

Chaque docstring reprend la phrase du brief : Étant donné / quand / alors.
Les 10 tests fournis s'arrêtent à la mécanique vérifiable sans trafic réel ;
ceux-ci couvrent les deux boucles restantes, la distribution du score et la
traçabilité des ajustements de seuil.
"""
from __future__ import annotations

import time

import pytest

from app.llm_client import Bundle
from app.telemetry import Mesure

CLAUSES_C01 = ["durée", "prix et paiement", "résiliation", "confidentialité", "droit applicable"]


@pytest.fixture(autouse=True)
def jeu_eval_isole(tmp_path, monkeypatch):
    """Aucun test ne doit écrire dans le vrai jeu d'évaluation ni dans les seuils.

    La boucle 3 verse des fichiers dans ``eval/contrats/`` et des lignes dans
    ``eval/attendus.jsonl`` — c'est son travail. Sans cette isolation, un test
    pollue le dépôt et fait échouer le gate des exécutions suivantes.
    """
    monkeypatch.setenv("CAPTURES_PATH", str(tmp_path / "captures.jsonl"))
    monkeypatch.setenv("CONTRATS_PATH", str(tmp_path / "contrats_enrichis"))
    monkeypatch.setenv("ATTENDUS_PATH", str(tmp_path / "attendus_enrichis.jsonl"))
    monkeypatch.setenv("SEUILS_PATH", str(tmp_path / "seuils.json"))


@pytest.fixture
def canary_v2(registry):
    """Une v2 étiquetée avec sa confiance de référence, déployée en canary à 10 %."""
    from ops.deploy import deployer_canary

    registry.etiqueter(
        "v2.0.0",
        Bundle.charger("v2"),
        commit="abc1234",
        note_eval=0.95,
        details={"confiance_moyenne": 0.80},
    )
    deployer_canary("v2.0.0", pourcentage=10, registry=registry)
    return registry


def _servir(metriques, version="v2.0.0", n=40, score=0.85, latence=2500.0, erreur=False):
    for _ in range(n):
        metriques.enregistrer(
            Mesure(
                ts=time.time(), version=version, route="/analyse",
                latence_ms=latence, score=None if erreur else score,
                erreur=erreur, cout_eur=0.02,
            )
        )


# --------------------------------------------------------- tableau de bord


def test_dashboard_distribution_du_score(metriques, registry):
    """Étant donné le tableau de bord, quand on l'ouvre, alors la **distribution**
    du score de confiance y est visible — pas seulement sa moyenne."""
    from ops.dashboard import resume

    # Distribution bimodale : moitié excellente, moitié à refaire. La moyenne
    # (0,70) serait rassurante alors que la moitié du travail est à reprendre.
    _servir(metriques, n=10, score=0.95)
    _servir(metriques, n=10, score=0.45)

    v2 = resume(metriques, fenetre_s=60, registry=registry)["par_version"]["v2.0.0"]
    assert abs(v2["score_moyen"] - 0.70) < 0.01, "la moyenne seule est trompeuse ici"

    distribution = v2["distribution_score"]
    assert sum(distribution.values()) == 20
    assert distribution["0,85 et plus - exploitable"] == 10
    assert distribution["moins de 0,50 - a refaire"] == 10
    assert distribution["0,70 a 0,85 - a verifier"] == 0
    # Les percentiles disent ce que la moyenne cache.
    assert v2["score_p10"] < 0.5 < v2["score_p90"]


# ------------------------------------------- boucle 2 : promotion pilotée


def test_promotion_canary_si_metriques_conformes(canary_v2, metriques):
    """Étant donné des métriques canary conformes aux critères, quand la fenêtre
    d'observation s'achève, alors la v2 est promue au palier suivant et
    l'événement est tracé."""
    from ops.deploy import promouvoir_si_conforme

    _servir(metriques, n=40, score=0.85)  # au-dessus de la référence du gate (0,80)
    resultat = promouvoir_si_conforme(canary_v2, metriques, fenetre_s=60, minimum=30)

    assert resultat["promue"] is True
    assert resultat["palier"] == 30
    assert canary_v2.canary() == ("v2.0.0", 30)
    entree = canary_v2.journal()[-1]
    assert entree["evenement"] == "promotion_canary"
    assert entree["avant"]["canary_percent"] == 10 and entree["apres"]["canary_percent"] == 30
    assert entree["signal"] == "score de confiance"


def test_pas_de_promotion_si_metriques_non_conformes(canary_v2, metriques):
    """Étant donné des métriques non conformes, quand la fenêtre s'achève, alors
    la promotion n'a pas lieu et le refus est tracé avec son motif."""
    from ops.deploy import promouvoir_si_conforme

    # 0,72 : au-dessus du seuil de rollback (0,70) mais sous la référence du
    # gate (0,80). La version n'est pas en train de casser, elle n'a simplement
    # pas prouvé qu'elle tenait ce qu'elle promettait : on ne monte pas.
    _servir(metriques, n=40, score=0.72)
    resultat = promouvoir_si_conforme(canary_v2, metriques, fenetre_s=60, minimum=30, marge=0.05)

    assert resultat["promue"] is False
    assert "référence du gate" in resultat["motif"]
    assert canary_v2.canary() == ("v2.0.0", 10), "le canary reste à son palier"
    assert canary_v2.journal()[-1]["evenement"] == "promotion_refusee"


def test_pas_de_promotion_sur_fenetre_incomplete(canary_v2, metriques):
    """Étant donné trop peu de trafic, quand la boucle s'exécute, alors elle
    s'abstient : promouvoir sur cinq requêtes reviendrait à tirer à pile ou face."""
    from ops.deploy import promouvoir_si_conforme

    _servir(metriques, n=5, score=0.95)
    resultat = promouvoir_si_conforme(canary_v2, metriques, fenetre_s=60, minimum=30)

    assert resultat["promue"] is False
    assert "fenêtre incomplète" in resultat["motif"]
    assert canary_v2.canary() == ("v2.0.0", 10)


def test_promotion_totale_au_dernier_palier(canary_v2, metriques):
    """Étant donné un canary au dernier palier avec des métriques conformes, quand
    la boucle s'exécute, alors la v2 devient active à 100 % et le canary est retiré."""
    from ops.deploy import deployer_canary, promouvoir_si_conforme

    deployer_canary("v2.0.0", pourcentage=30, registry=canary_v2)
    _servir(metriques, n=40, score=0.90)
    resultat = promouvoir_si_conforme(canary_v2, metriques, fenetre_s=60, minimum=30)

    assert resultat["promue"] is True and resultat["palier"] == 100
    assert canary_v2.active() == "v2.0.0"
    assert canary_v2.canary() == (None, 0)


# --------------------------------------- boucle 3 : enrichissement du jeu


def test_capture_faible_confiance_anonymisee(tmp_path, registry):
    """Étant donné un cas de production à faible confiance, quand il est capturé,
    alors il entre dans la file de validation **pseudonymisé**, jamais en clair."""
    from eval.enrichissement import capturer, file_attente

    captures = tmp_path / "captures.jsonl"
    contrat = (
        "Contrat conclu entre la société ACME FRANCE, SIREN 123 456 789, "
        "sise 12 rue de la Paix, contact jean.dupont@acme.fr, "
        "pour un montant de 45 000 €. Article 1 — Résiliation."
    )
    cas = capturer(contrat, confiance=0.42, version="v2.0.0", captures=captures, registry=registry)

    assert cas is not None and cas["statut"] == "a_valider"
    stocke = cas["contrat"]
    for secret in ("123 456 789", "jean.dupont@acme.fr", "45 000", "rue de la Paix"):
        assert secret not in stocke, f"{secret!r} ne doit pas être écrit sur disque"
    assert "Résiliation" in stocke, "le vocabulaire juridique doit survivre"
    assert cas["anonymisation"], "le relecteur doit voir ce qui a été retiré"
    assert [c["id"] for c in file_attente(captures)] == [cas["id"]]
    assert registry.journal()[-1]["evenement"] == "capture_faible_confiance"


def test_pas_de_capture_au_dessus_du_seuil(tmp_path):
    """Étant donné une analyse fiable, quand elle est servie, alors rien n'est capturé."""
    from eval.enrichissement import capturer, file_attente

    captures = tmp_path / "captures.jsonl"
    assert capturer("texte" * 20, confiance=0.91, version="v2.0.0", captures=captures) is None
    assert file_attente(captures) == []


def test_cas_valide_est_rejoue_par_le_gate(tmp_path, registry, historique, contrat):
    """Étant donné un cas de production à faible confiance, quand il est validé,
    alors il apparaît dans le jeu d'évaluation et la chaîne le rejoue à la
    fusion suivante."""
    from eval.enrichissement import capturer, valider
    from eval.run_eval import charger_attendus, evaluer

    captures, contrats, attendus = (
        tmp_path / "captures.jsonl", tmp_path / "contrats", tmp_path / "attendus.jsonl"
    )
    contrats.mkdir()
    (contrats / "c01.txt").write_text(contrat("c01"), encoding="utf-8")
    attendus.write_text(
        '{"contrat_id": "c01", "pages": 2, "clauses_attendues": '
        + str(CLAUSES_C01).replace("'", '"')
        + ', "seuil_note": 0.75}\n',
        encoding="utf-8",
    )

    cas = capturer(contrat("c01"), confiance=0.55, version="v2.0.0", captures=captures)
    entree = valider(
        cas["id"], ["résiliation", "durée"], "juriste.test",
        captures=captures, contrats=contrats, attendus=attendus, registry=registry,
    )

    # 1. le cas est dans le jeu d'évaluation, avec son contrat pseudonymisé
    assert entree["origine"] == "production" and entree["valide_par"] == "juriste.test"
    assert entree["contrat_id"] in charger_attendus(attendus)
    assert (contrats / f"{entree['contrat_id']}.txt").exists()
    assert registry.journal()[-1]["evenement"] == "enrichissement_jeu_evaluation"

    # 2. le gate le rejoue à l'exécution suivante, sans configuration supplémentaire
    rapport = evaluer("v2", contrats=contrats, attendus=attendus, historique=historique)
    assert entree["contrat_id"] in rapport.par_contrat
    assert rapport.par_contrat[entree["contrat_id"]]["note"] > 0


def test_cas_rejete_n_entre_pas_dans_le_jeu(tmp_path, registry):
    """Étant donné un cas capturé jugé inexploitable, quand il est rejeté, alors
    il quitte la file et n'entre pas dans le jeu d'évaluation."""
    from eval.enrichissement import capturer, file_attente, rejeter, valider

    captures = tmp_path / "captures.jsonl"
    cas = capturer("texte " * 30, confiance=0.3, version="v2.0.0", captures=captures)
    rejeter(cas["id"], "juriste.test", "document illisible", captures=captures, registry=registry)

    assert file_attente(captures) == []
    assert registry.journal()[-1]["evenement"] == "capture_rejetee"
    with pytest.raises(KeyError):
        valider(cas["id"], ["durée"], "juriste.test", captures=captures)


# ------------------------------------------ traçabilité des ajustements


def test_ajustement_de_seuil_trace(tmp_path, registry):
    """Étant donné le journal de pilotage, quand on consulte un ajustement de seuil,
    alors il y figure avec sa valeur avant/après, son auteur et son motif."""
    from ops.deploy import ajuster_seuil, charger_seuils

    chemin = tmp_path / "seuils.json"
    seuils = ajuster_seuil(
        "score_min", 0.75, auteur="eq-ia", motif="P10 observé à 0,78 sur 7 jours",
        registry=registry, chemin=chemin,
    )

    assert seuils["score_min"] == 0.75
    assert charger_seuils(chemin)["score_min"] == 0.75
    entree = registry.journal()[-1]
    assert entree["evenement"] == "ajustement_seuil"
    assert entree["avant"] == {"score_min": 0.70} and entree["apres"] == {"score_min": 0.75}
    assert entree["auteur"] == "eq-ia" and "P10" in entree["motif"]


def test_seuil_inconnu_refuse(tmp_path, registry):
    """Un seuil qui n'existe pas est refusé plutôt qu'écrit silencieusement."""
    from ops.deploy import ErreurDeploiement, ajuster_seuil

    with pytest.raises(ErreurDeploiement):
        ajuster_seuil(
            "score_minimum", 0.75, auteur="eq-ia", motif="faute de frappe",
            registry=registry, chemin=tmp_path / "seuils.json",
        )


# ------------------------------------------------------------- frontend


def test_frontend_servi(client):
    """Étant donné le frontend, quand on l'ouvre, alors le client et le tableau de
    bord répondent, et le tableau de bord expose la distribution du score."""
    assert "<title>Mardik" in client.get("/").text
    assert client.get("/pilotage").status_code == 200
    assert client.get("/web/commun.css").headers["content-type"].startswith("text/css")

    pilotage = client.get("/api/pilotage?fenetre=600").json()
    assert {"total", "par_version", "index", "journal"} <= set(pilotage)
    assert client.get("/api/captures").json() == {"attente": []}
    assert len(client.get("/api/exemple").json()["texte"]) > 10000


def test_analyse_de_bout_en_bout_par_le_frontend(client, registry, contrat):
    """Un contrat long envoyé par le frontend traverse la gateway et revient avec
    ses clauses et son indice de fiabilité."""
    from ops.deploy import promouvoir

    registry.etiqueter("v2.0.0", Bundle.charger("v2"), commit="abc1234", note_eval=0.95)
    promouvoir("v2.0.0", registry=registry)

    r = client.post("/analyse", json={"texte": contrat("c12")})
    assert r.status_code == 200
    corps = r.json()
    assert r.headers["x-mardik-version"] == "v2.0.0"
    assert corps["confiance_globale"] > 0 and corps["sections"] > 1
    assert {"résiliation", "droit applicable"} <= {c["type"] for c in corps["clauses"]}
