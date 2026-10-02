"""Amorce un registre de démonstration au démarrage (hébergement éphémère).

Sur un hébergement sans disque persistant, ``ops/registry`` repart vide à chaque
redémarrage et la gateway répond « aucune version active ». Ce script remet la
chaîne dans son état de démonstration : v1.0.0 active, v2.0.0 publiée par le
gate, canary à 20 %.

**Il ne contourne pas le gate.** ``ops.deploy.publier`` est appelé normalement :
si la v2 ne passe pas l'évaluation, l'amorçage échoue et le service démarre
avec la seule v1 — ce qui est le comportement correct, pas un détail de démo.
Le gate tourne sur un sous-ensemble de contrats pour ne pas retarder le
démarrage ; c'est la seule concession, et elle est explicite.

Sans effet si le registre contient déjà une version active.
"""
from __future__ import annotations

import os
import sys

from app.llm_client import Bundle
from ops.deploy import ErreurDeploiement, deployer_canary, publier
from ops.registry import ErreurRegistre, Registry

CONTRATS_AMORCAGE = ["c01", "c02", "c07"]


def amorcer(registry: Registry | None = None, *, pourcentage: int = 20) -> dict:
    registre = registry or Registry()
    # La garde porte sur la v2, pas sur « un registre non vide » : l'image
    # embarque déjà la v1.0.0 étiquetée, donc tester l'active ferait croire que
    # tout est en place alors que la version à démontrer n'est pas publiée.
    if "v2.0.0" in registre.versions():
        return {"amorce": False, "motif": "v2.0.0 déjà publiée"}

    if "v1.0.0" not in registre.versions():
        registre.etiqueter("v1.0.0", Bundle.charger("v1"), commit="historique", note_eval=None)
    registre.definir_actif("v1.0.0")

    try:
        from eval.run_eval import evaluer

        rapport = evaluer("v2", sous_ensemble=CONTRATS_AMORCAGE, historique=None)
        publier("v2.0.0", bundle="v2", commit=os.environ.get("COMMIT", "demo"),
                registry=registre, rapport=rapport)
        deployer_canary("v2.0.0", pourcentage=pourcentage, registry=registre)
    except (ErreurDeploiement, ErreurRegistre) as exc:
        return {"amorce": True, "canary": False, "motif": f"v2 non publiée : {exc}"}

    return {"amorce": True, "canary": True, "active": "v1.0.0", "pourcentage": pourcentage}


if __name__ == "__main__":
    print(amorcer(), file=sys.stderr)
