"""Pseudonymisation des contrats capturés avant leur entrée dans le jeu d'évaluation.

Pourquoi c'est indispensable ici : un cas capturé en production est versé dans
``eval/contrats/`` et ``eval/attendus.jsonl``, donc **commité dans Git et rejoué
par l'intégration continue**. Sans traitement, des contrats clients réels se
retrouveraient dans un dépôt et dans une infrastructure tierce. Le produit
analyse des contrats commerciaux — dont les clauses de confidentialité : la
contradiction serait totale.

**Ce qu'on retire et ce qu'on garde.** Le jeu d'évaluation ne sert qu'à vérifier
que le modèle retrouve les bonnes clauses. Il a besoin de la *structure
juridique* et du *vocabulaire*, jamais de l'identité des parties ni des montants
réels. On peut donc pseudonymiser agressivement sans perdre la valeur
d'évaluation : « [MONTANT] € » reste un montant pour qui cherche une clause de
prix.

**Limite assumée.** Ce module repose sur des expressions régulières, pas sur une
reconnaissance d'entités nommées. Il traite ce qui a une forme reconnaissable
(SIREN, IBAN, montants, courriels, adresses) et laissera passer une raison
sociale écrite en toutes lettres au fil du texte. C'est pourquoi la **relecture
humaine avant versement** (``eval/enrichissement.py``) fait aussi office de
contrôle de confidentialité : les deux rôles sont tenus par la même étape, et
c'est ce qui rend le dispositif défendable devant un juriste.
"""
from __future__ import annotations

import re

# Chaque motif est remplacé par un marqueur qui conserve la *nature* de
# l'information retirée : la phrase reste grammaticalement lisible et le
# vocabulaire juridique intact, donc le cas garde sa valeur d'évaluation.
REGLES: tuple[tuple[re.Pattern[str], str], ...] = (
    # Coordonnées bancaires — avant les autres motifs numériques.
    (re.compile(r"\b[A-Z]{2}\d{2}(?:[\s]?[A-Z0-9]{4}){2,7}\b"), "[IBAN]"),
    # Courriels.
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"), "[COURRIEL]"),
    # Téléphones français.
    (re.compile(r"\b0[1-9](?:[\s.-]?\d{2}){4}\b"), "[TELEPHONE]"),
    # SIRET (14 chiffres) puis SIREN (9 chiffres), éventuellement espacés.
    (re.compile(r"\b\d{3}[\s.]?\d{3}[\s.]?\d{3}[\s.]?\d{5}\b"), "[SIRET]"),
    (re.compile(r"\b\d{3}[\s.]?\d{3}[\s.]?\d{3}\b"), "[SIREN]"),
    # Immatriculation au registre du commerce.
    (re.compile(r"\bRCS\s+[A-ZÀ-Ý][\w-]+", re.IGNORECASE), "RCS [VILLE]"),
    # Montants : on retire le chiffre, on garde l'unité — « euros » reste détectable.
    (re.compile(r"\b\d[\d\s.,]*\s*(?=(?:€|euros?|EUR)\b)", re.IGNORECASE), "[MONTANT] "),
    (re.compile(r"\b\d[\d\s.,]*\s*€"), "[MONTANT] €"),
    # Adresse postale : numéro + voie, puis code postal + commune.
    (
        re.compile(
            r"\b\d{1,4}(?:\s?(?:bis|ter))?\s+"
            r"(?:rue|avenue|boulevard|place|impasse|chemin|allée|quai|cours)\s+"
            r"[^,.\n]{2,40}",
            re.IGNORECASE,
        ),
        "[ADRESSE]",
    ),
    (re.compile(r"\b\d{5}\s+[A-ZÀ-Ý][\w-]+(?:[\s-][A-ZÀ-Ý][\w-]+)*"), "[CODE POSTAL] [VILLE]"),
    # Raison sociale suivant une forme juridique. Les deux graphies de « société »
    # sont prévues : les contrats numérisés perdent souvent leurs accents à la
    # reconnaissance de caractères, et un motif accentué seul les laisserait passer.
    (
        re.compile(
            r"\b(soci[ée]t[ée]|SAS|SASU|SARL|SA|SCI|EURL)\s+"
            r"([A-ZÀ-Ý][\w&'-]*(?:\s+[A-ZÀ-Ý][\w&'-]*){0,3})",
        ),
        r"\1 [RAISON SOCIALE]",
    ),
    # Raison sociale PRÉCÉDANT la forme juridique (« Mardik SAS »), l'ordre le plus
    # courant en français. Sans ce motif, le nom de la partie restait en clair —
    # c'est exactement ce que la relecture humaine est censée rattraper, mais
    # autant ne pas lui laisser les cas les plus fréquents.
    (
        re.compile(
            r"\b([A-ZÀ-Ý][\w&'-]*(?:\s+[A-ZÀ-Ý][\w&'-]*){0,3})\s+"
            r"(SAS|SASU|SARL|SA|SCI|EURL|SNC)\b",
        ),
        r"[RAISON SOCIALE] \2",
    ),
    # Personne physique introduite par une civilité.
    (
        re.compile(
            r"\b(Monsieur|Madame|Mme|M\.|Me|Maître|Maitre)\s+"
            r"([A-ZÀ-Ý][\w'-]*(?:\s+[A-ZÀ-Ý][\w'-]*){0,2})",
        ),
        r"\1 [NOM]",
    ),
)


def anonymiser(texte: str) -> tuple[str, dict[str, int]]:
    """Pseudonymise un contrat et rend le compte des substitutions par catégorie.

    Le compte est renvoyé pour être **affiché au relecteur** : il doit savoir ce
    qui a été retiré automatiquement pour juger de ce qui pourrait rester. Un cas
    où le compte est vide sur un contrat de 30 pages est suspect — pas rassurant.
    """
    resultat = texte
    comptes: dict[str, int] = {}
    for motif, remplacement in REGLES:
        resultat, substitutions = motif.subn(remplacement, resultat)
        if substitutions:
            etiquette = re.sub(r"[^A-ZÀ-Ý ]", "", remplacement).strip() or remplacement
            comptes[etiquette] = comptes.get(etiquette, 0) + substitutions
    return resultat, comptes
