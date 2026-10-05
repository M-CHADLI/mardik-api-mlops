"""Tests d'acceptance — la chaîne LLMOps (5 tests, rouges au départ sauf « client v1 »).

Chaque docstring reprend la phrase du brief : Étant donné / quand / alors.
"""
from __future__ import annotations

import pytest

from app.llm_client import Bundle
from scripts.client_v1 import verifier


def _poster(client):
    def poster(chemin, corps):
        r = client.post(chemin, json=corps)
        return r.status_code, r.json()

    return poster


def test_contrat_v2_long_analyse_sans_troncature(client, contrat):
    """Étant donné un contrat long (30 pages et plus), quand on l'envoie sur /v2/analyse,
    alors la réponse respecte le contrat v2 (clauses typées avec extrait et score de
    confiance, score global) et couvre les clauses de la fin du document — là où la
    v1 tronque."""
    texte = contrat("c12")
    reponse_v1 = client.post("/v1/analyse", json={"texte": texte}).json()
    assert reponse_v1["tronque"] is True, "le contrat de démo doit dépasser la fenêtre v1"

    r = client.post("/v2/analyse", json={"texte": texte, "contrat_id": "c12"})
    assert r.status_code == 200, r.text
    corps = r.json()
    assert {"clauses", "confiance_globale", "modele", "version", "sections"} <= set(corps)
    assert corps["version"].startswith("v2")
    assert corps["sections"] > 1, "un contrat long doit être découpé en plusieurs sections"
    types = {c["type"] for c in corps["clauses"]}
    for clause in corps["clauses"]:
        assert {"type", "extrait", "confiance"} <= set(clause)
        assert 0.0 <= clause["confiance"] <= 1.0
    assert 0.0 <= corps["confiance_globale"] <= 1.0
    # les clauses en fin de contrat, invisibles pour la v1, sont bien trouvées
    assert {"résiliation", "droit applicable"} <= types
    assert "résiliation" not in reponse_v1["clauses"] or "droit applicable" not in reponse_v1["clauses"]
    assert len(types) == len(corps["clauses"]), "pas de doublon de type après consolidation"


def test_erreurs_explicites_jamais_de_500(client, contrat, monkeypatch):
    """Étant donné une requête invalide ou un fournisseur LLM en panne, quand on appelle
    /v2/analyse, alors l'API renvoie une erreur explicite (4xx/5xx avec « detail »)
    et ne plante jamais (pas de 500 brut)."""
    r = client.post("/v2/analyse", json={"pas_le_bon_champ": 1})
    assert r.status_code == 422
    assert "detail" in r.json()

    r = client.post("/v2/analyse", json={"texte": "trop court"})
    assert r.status_code == 422

    # fournisseur en panne : MOCK=off + proxy injoignable
    monkeypatch.setenv("MOCK", "off")
    monkeypatch.setenv("LLM_PROXY_URL", "http://127.0.0.1:9")  # port fermé
    monkeypatch.setenv("LLM_TIMEOUT_S", "2")
    r = client.post("/v2/analyse", json={"texte": contrat("c01")})
    assert r.status_code == 503, r.text
    assert "detail" in r.json() and "LLM" in r.json()["detail"]


def test_client_v1_fonctionne(client, contrat):
    """Étant donné le client historique, quand il appelle /v1/analyse comme aujourd'hui,
    alors il obtient exactement le contrat v1 (clauses, modele, version) — avant,
    pendant et après la livraison de la v2."""
    assert verifier(_poster(client), contrat("c01")) == []


def test_etiquetage_version_apres_gate(registry, historique, monkeypatch):
    """Étant donné un bundle v2 dont le gate d'évaluation passe, quand on le publie,
    alors une version étiquetée (vX.Y.Z) apparaît dans le registre avec son manifeste
    (commit, empreinte de la config, note d'éval, date), et une entrée « publication »
    est journalisée. Un bundle dont le gate échoue est refusé."""
    from eval.run_eval import evaluer
    from ops.deploy import ErreurDeploiement, publier

    rapport = evaluer("v2", sous_ensemble=["c01", "c02", "c07"], historique=historique)
    assert rapport.passe, rapport.motifs
    manifest = publier("v2.0.0", bundle="v2", commit="abc1234", registry=registry, rapport=rapport)

    assert "v2.0.0" in registry.versions()
    assert manifest["commit"] == "abc1234"
    assert manifest["empreinte"] == Bundle.charger("v2").empreinte()
    assert manifest["note_eval"] == rapport.note
    assert manifest["date"]
    assert registry.bundle("v2.0.0").strategie == "map_reduce_clauses"
    assert registry.journal()[-1]["evenement"] == "publication"

    # un gate en échec bloque la publication
    rapport.passe, rapport.motifs = False, ["note 0.4 < seuil 0.75"]
    with pytest.raises(ErreurDeploiement):
        publier("v2.0.1", bundle="v2", registry=registry, rapport=rapport)
    assert "v2.0.1" not in registry.versions()


def test_gate_evaluation_note_par_version(historique):
    """Étant donné les 12 contrats annotés, quand on exécute le gate d'évaluation sur
    la v1 puis sur la v2, alors chaque exécution produit une note par contrat et une
    note globale enregistrées dans eval/history.jsonl, la v2 passe le seuil et fait
    mieux que la v1 sur les contrats longs."""
    from eval.run_eval import evaluer

    v1 = evaluer("v1", seuil=0.75, historique=historique)
    v2 = evaluer("v2", seuil=0.75, historique=historique)

    assert set(v2.par_contrat) == {f"c{i:02d}" for i in range(1, 13)}
    assert all("note" in c and "seuil_note" in c for c in v2.par_contrat.values())
    assert v2.passe, v2.motifs
    assert v2.note >= 0.75
    for long_ in ("c07", "c10", "c12"):
        assert v2.par_contrat[long_]["note"] > v1.par_contrat[long_]["note"]
    lignes = historique.read_text(encoding="utf-8").strip().splitlines()
    assert len(lignes) == 2
    assert '"version": "v1.0.0"' in lignes[0] and '"version": "v2.0.0"' in lignes[1]
