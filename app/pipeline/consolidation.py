"""Fusion et dédoublonnage des clauses extraites section par section (*reduce*).

Contrat attendu :

    consolider(par_section: list[list[Clause]]) -> list[Clause]

* Deux clauses du même ``type`` trouvées dans des sections différentes sont
  **une seule** clause dans le résultat : on garde l'extrait le plus long (le
  plus informatif), on fusionne les ``sections`` et on retient la
  ``confiance_llm`` maximale déclarée.
* L'ordre de sortie suit l'ordre d'apparition dans le contrat (première
  section où la clause a été vue).
* Le résultat ne contient jamais deux clauses de même type.

Le découpage multiplie les occurrences — une clause de résiliation est souvent
évoquée dans l'article qui la porte *et* dans les renvois d'autres articles.
Sans cette étape, un contrat de 40 pages rendrait une liste de clauses plus
longue que le contrat lui-même. Les ``sections`` fusionnées ne sont pas qu'une
trace de provenance : elles alimentent le signal de redondance du score de
confiance.
"""
from __future__ import annotations

from app.pipeline.confiance import Clause


def consolider(par_section: list[list[Clause]]) -> list[Clause]:
    """Une clause par type, dans l'ordre d'apparition dans le contrat."""
    fusionnees: dict[str, Clause] = {}
    for clauses in par_section:
        for clause in clauses:
            retenue = fusionnees.get(clause.type)
            if retenue is None:
                fusionnees[clause.type] = Clause(
                    type=clause.type,
                    extrait=clause.extrait,
                    confiance_llm=clause.confiance_llm,
                    sections=list(clause.sections),
                )
                continue
            if len(clause.extrait) > len(retenue.extrait):
                retenue.extrait = clause.extrait
            retenue.confiance_llm = max(retenue.confiance_llm, clause.confiance_llm)
            for indice in clause.sections:
                if indice not in retenue.sections:
                    retenue.sections.append(indice)
    for clause in fusionnees.values():
        clause.sections.sort()
    return list(fusionnees.values())
