"""Découpage d'un contrat par clauses (étape *map* du map-reduce).

Contrat attendu :

    decouper(texte: str, taille_max: int = 6000) -> list[Section]

* Une ``Section`` porte un ``titre`` (l'intitulé de l'article, ou ``"préambule"``),
  un ``texte`` et son ``indice`` (ordre dans le contrat).
* Le découpage suit les intitulés d'articles (« Article 3 — Résiliation »,
  « 3. Résiliation », « ARTICLE 3 : … »). Une section plus longue que
  ``taille_max`` est elle-même découpée en morceaux, sans couper une phrase.
* La concaténation des ``texte`` de toutes les sections doit couvrir tout le
  contrat : rien ne doit être perdu — c'est précisément ce que la v1 ne
  garantit pas.

Invariant tenu par l'implémentation : ``"".join(s.texte for s in decouper(t)) == t``.
Le découpage travaille sur des **indices de lignes** et ne recompose jamais le
texte à la main, ce qui rend la propriété vraie par construction plutôt que par
vérification — c'est la garantie « aucune troncature » sur laquelle repose la v2.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Un intitulé d'article : « Article 3 — Résiliation », « ARTICLE 12 : … »,
# « 3. Résiliation », « 3.1 Confidentialité ». On exige une ligne courte pour ne
# pas confondre avec une phrase qui commencerait par un nombre (« 30 jours… »).
MOTIF_INTITULE = re.compile(
    r"""^\s*
        (?:
            (?:article|titre|chapitre|annexe|préambule|preambule)\s*\d*[a-z]?
          | \d+(?:\.\d+)*
        )
        \s*(?:[-–—:.)]+\s*|\s+)
        \S
    """,
    re.IGNORECASE | re.VERBOSE,
)
LONGUEUR_MAX_INTITULE = 120
SEPARATEURS_TITRE = " \t-–—:.)"


@dataclass
class Section:
    indice: int
    titre: str
    texte: str


def decouper(texte: str, taille_max: int = 6000) -> list[Section]:
    """Découpe ``texte`` en sections d'au plus ``taille_max`` caractères."""
    lignes = texte.splitlines(keepends=True)
    if not lignes:
        return [Section(indice=0, titre="préambule", texte=texte)]

    debuts = [n for n, ligne in enumerate(lignes) if est_intitule(ligne)]
    # Ce qui précède le premier article (parties, objet, visas) est le préambule.
    if not debuts or debuts[0] != 0:
        debuts.insert(0, 0)

    sections: list[Section] = []
    for rang, debut in enumerate(debuts):
        fin = debuts[rang + 1] if rang + 1 < len(debuts) else len(lignes)
        bloc = "".join(lignes[debut:fin])
        titre = titre_de(lignes[debut]) if est_intitule(lignes[debut]) else "préambule"
        morceaux = _morceler(bloc, taille_max)
        for rang_morceau, morceau in enumerate(morceaux, start=1):
            libelle = (
                f"{titre} ({rang_morceau}/{len(morceaux)})" if len(morceaux) > 1 else titre
            )
            sections.append(Section(indice=len(sections), titre=libelle, texte=morceau))
    return sections


def est_intitule(ligne: str) -> bool:
    nue = ligne.strip()
    if not nue or len(nue) > LONGUEUR_MAX_INTITULE:
        return False
    return bool(MOTIF_INTITULE.match(nue))


def titre_de(ligne: str) -> str:
    return ligne.strip().strip(SEPARATEURS_TITRE) or "section"


def _morceler(bloc: str, taille_max: int) -> list[str]:
    """Coupe ``bloc`` en morceaux ≤ ``taille_max``, de préférence en fin de phrase.

    La concaténation des morceaux reconstitue ``bloc`` exactement : on ne coupe
    qu'en déplaçant un curseur, jamais en nettoyant ou en réécrivant le texte.
    """
    if len(bloc) <= taille_max:
        return [bloc]
    morceaux: list[str] = []
    reste = bloc
    while len(reste) > taille_max:
        fenetre = reste[:taille_max]
        coupe = max(fenetre.rfind(". "), fenetre.rfind(".\n"), fenetre.rfind("\n\n"))
        if coupe < taille_max // 3:  # pas de fin de phrase exploitable : fin de ligne
            coupe = fenetre.rfind("\n")
        if coupe < taille_max // 3:  # ni l'un ni l'autre : coupe franche, en dernier recours
            coupe = taille_max - 1
        morceaux.append(reste[: coupe + 1])
        reste = reste[coupe + 1 :]
    if reste:
        morceaux.append(reste)
    return morceaux
