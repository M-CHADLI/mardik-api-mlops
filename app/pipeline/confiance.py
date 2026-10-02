"""Score de confiance composite, par clause puis global.

Contrat attendu :

    scorer(clauses: list[Clause], texte: str) -> tuple[list[Clause], float]

Deux signaux **indépendants** entrent dans le score :

1. ``confiance_llm`` — ce que le modèle déclare. Seul, c'est un signal creux :
   un LLM est tout aussi sûr de lui sur une citation inventée.
2. l'**ancrage** — l'extrait cité figure-t-il réellement dans le contrat ?
   Cette vérification se fait sans le modèle, donc elle ne partage aucun biais
   avec lui. C'est elle qui rattrape l'hallucination de citation.

Le score composite les combine en **plafonnant** la confiance déclarée par
l'ancrage plutôt qu'en les moyennant :

    confiance = confiance_llm × (0.35 + 0.65 × ancrage) + bonus_redondance

Une moyenne laisserait une citation inventée (ancrage 0) à 0,48 pour un modèle
sûr à 0,95 — un score « moyen », donc ambigu. Le produit la fait tomber à 0,33 :
le juriste voit tout de suite qu'il faut relire. À l'inverse, une clause bien
citée et vue dans plusieurs sections du contrat gagne un petit bonus (+0,05) :
la redondance est un troisième signal, faible mais gratuit.

Le score **global** n'est pas la moyenne : ``0,7 × moyenne + 0,3 × minimum``.
Une moyenne noierait une clause douteuse parmi onze bonnes, alors que c'est
exactement ce que le client veut voir (« savoir quand une relecture s'impose »).
Pondérer le maillon faible fait descendre le score global dès qu'une clause
est fragile, sans pour autant le réduire à ce seul cas.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

POIDS_PLANCHER = 0.35       # part de la confiance déclarée qui survit sans ancrage
BONUS_REDONDANCE = 0.05     # clause vue dans plusieurs sections
POIDS_MOYENNE = 0.7         # score global = 0,7 × moyenne + 0,3 × minimum
LONGUEUR_MOT_SIGNIFIANT = 4


@dataclass
class Clause:
    type: str
    extrait: str
    confiance_llm: float
    sections: list[int] = field(default_factory=list)
    confiance: float = 0.0

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "extrait": self.extrait,
            "confiance": round(self.confiance, 3),
            "sections": self.sections,
        }


def scorer(clauses: list[Clause], texte: str) -> tuple[list[Clause], float]:
    """Calcule la confiance composite de chaque clause, puis le score global."""
    reference = _normaliser(texte)
    for clause in clauses:
        ancrage = mesurer_ancrage(clause.extrait, reference)
        brut = clause.confiance_llm * (POIDS_PLANCHER + (1 - POIDS_PLANCHER) * ancrage)
        if len(clause.sections) > 1:
            brut += BONUS_REDONDANCE
        clause.confiance = round(min(1.0, max(0.0, brut)), 4)
    if not clauses:
        return clauses, 0.0
    scores = [c.confiance for c in clauses]
    moyenne = sum(scores) / len(scores)
    global_ = POIDS_MOYENNE * moyenne + (1 - POIDS_MOYENNE) * min(scores)
    return clauses, round(min(1.0, max(0.0, global_)), 4)


def mesurer_ancrage(extrait: str, texte_normalise: str) -> float:
    """1.0 si l'extrait est cité mot pour mot, sinon la part de ses mots retrouvés.

    L'ancrage partiel évite de punir une reformulation mineure (ponctuation,
    césure) aussi durement qu'une citation fabriquée de toutes pièces.
    """
    extrait_normalise = _normaliser(extrait)
    if not extrait_normalise:
        return 0.0
    if extrait_normalise in texte_normalise:
        return 1.0
    mots = {m for m in re.findall(r"\w+", extrait_normalise) if len(m) >= LONGUEUR_MOT_SIGNIFIANT}
    if not mots:
        return 0.0
    retrouves = sum(1 for mot in mots if mot in texte_normalise)
    return round(retrouves / len(mots), 4)


def _normaliser(texte: str) -> str:
    """Minuscules + espaces normalisés : la citation d'un LLM garde rarement la mise en page."""
    return re.sub(r"\s+", " ", texte.lower()).strip()
