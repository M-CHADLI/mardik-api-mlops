# Livrables — Mardik v2

*Brief « Du prototype au produit » — chaîne LLMOps et observabilité.
Index du rendu : chaque livrable attendu, où il se trouve, et comment le vérifier.*

| | |
|---|---|
| **Application en ligne** | **https://mardik-api.onrender.com/** — [client](https://mardik-api.onrender.com/) · [pilotage](https://mardik-api.onrender.com/pilotage) · [API](https://mardik-api.onrender.com/docs) |
| **Dépôt** | https://github.com/M-CHADLI/mardik-api-mlops |
| **Pull request** | [#1 — Chaîne LLMOps v2](https://github.com/M-CHADLI/mardik-api-mlops/pull/1) |

## Contenu de ce dossier

Les fichiers sont regroupés ici sous les cinq intitulés du brief. Ce sont des
**copies** : la source dans le dépôt fait foi, et `python -m livrables.assembler`
les régénère (voir `INSTANTANE.txt` pour le commit de référence).

```
livrables/
├── INSTANTANE.txt            commit et date de l'assemblage
├── 1-dossier-de-conception/  conception + expression de besoin
├── 2-api-v2/                 API v2, pipeline, gateway, bundles, client v1
├── 3-chaine-llmops/          workflow, deploy, gate, registre, Dockerfile
├── 4-observabilite/          dashboard, 3 boucles, télémétrie, exploitation
├── 5-frontend/               pages, routes, déploiement
├── 6-tests/                  les 10 d'acceptance fournis + les 13 ajoutés
└── assembler.py              régénère l'instantané
```

Les sections ci-dessous renvoient aux **originaux**, qui restent la référence.

---

## 1. Dossier de conception

**[`docs/dossier-de-conception.md`](../docs/dossier-de-conception.md)**

Les six pièces demandées, dans cet ordre : synthèse du besoin (fonctionnel /
contraintes / hors périmètre), langage du domaine, choix d'outils non triviaux,
contrat d'API v2, schéma de la chaîne LLMOps, schéma de la boucle
d'observabilité, et le tableau de pilotage *signal × seuil × rétroaction ×
trace*. Se termine par les questions ouvertes du brief et les réponses retenues.

---

## 2. L'API v2 du modèle

Documents longs, score de confiance, erreurs explicites — **v1 intacte**.

| Fichier | Rôle |
|---|---|
| [`app/api_v2.py`](../app/api_v2.py) | contrat `/v2`, orchestration map-reduce, appels de sections parallélisés |
| [`app/pipeline/decoupage.py`](../app/pipeline/decoupage.py) | découpage par article, invariant « rien n'est perdu » |
| [`app/pipeline/extraction.py`](../app/pipeline/extraction.py) | un appel LLM par section, sortie JSON contrainte |
| [`app/pipeline/consolidation.py`](../app/pipeline/consolidation.py) | fusion et dédoublonnage |
| [`app/pipeline/confiance.py`](../app/pipeline/confiance.py) | score composite : confiance déclarée × ancrage de l'extrait |
| [`app/gateway.py`](../app/gateway.py) | routage canary, en-tête `X-Mardik-Version` |
| [`models/v2/config.yaml`](../models/v2/config.yaml) | **le bundle = la version** |
| [`app/api_v1.py`](../app/api_v1.py) | **non modifié** — le contrat historique |

**Vérification :** `python scripts/client_v1.py` sort en 0, et le test
`test_client_v1_fonctionne` tourne à chaque fusion.

---

## 3. La chaîne LLMOps

**[`.github/workflows/llmops.yml`](../.github/workflows/llmops.yml)**

```
PR / push main ──► lint ──► tests ──► gate d'évaluation (MOCK) ──► build
                                                                     │
tag vX.Y.Z ──────────────────────────────────────────────────────────┤
                                                                     ▼
              gate MODÈLE RÉEL ──► étiquetage registre ──► canary 10 %
```

| Fichier | Rôle |
|---|---|
| [`eval/run_eval.py`](../eval/run_eval.py) | le gate : note, latence P95, coût, dispersion entre passes |
| [`ops/deploy.py`](../ops/deploy.py) | publier · canary · promouvoir · rollback · surveiller |
| [`ops/registry/`](../ops/registry/) | artefacts étiquetés, manifestes, `index.json` |

**Ce qui bloque :** `ops.deploy.publier` rejoue le gate et refuse l'étiquetage
s'il échoue. Aucun chemin vers le registre ne contourne la vérification, ni par
la CI, ni par la console.

**Ce que la CI vérifie sans le vrai modèle :** la mécanique (déterministe,
gratuite, donc légitime pour bloquer une PR). La note du modèle relève du gate
de release, sur tag.

---

## 4. L'observabilité qui pilote

### Tableau de bord — [`ops/dashboard.py`](../ops/dashboard.py)

Trafic v1/v2, latence P50/P95, taux d'erreur, coût, et la **distribution** du
score de confiance (tranches métier + P10/P50/P90), pas seulement sa moyenne.

> Dix analyses à 0,95 et dix à 0,45 donnent la même moyenne que vingt à 0,70.
> Seule la distribution distingue ces deux situations.

### Les trois boucles de rétroaction

Les trois sont **déclenchables depuis le tableau de bord en ligne** (boutons
« Boucle 1 » et « Boucle 2 », formulaire de relecture pour la boucle 3), en plus
de la ligne de commande.

| # | Boucle | Code | Déclencheur |
|---|---|---|---|
| 1 | **Rollback sur signal** | [`ops/deploy.py::surveiller`](../ops/deploy.py) | confiance < 0,70, erreurs > 10 %, ou P95 > 8 s — automatique |
| 2 | **Promotion canary pilotée** | [`ops/deploy.py::promouvoir_si_conforme`](../ops/deploy.py) | confiance ≥ référence du gate − marge, sur ≥ 30 mesures → 10 → 30 → 100 % |
| 3 | **Enrichissement du jeu d'éval** | [`eval/enrichissement.py`](../eval/enrichissement.py) | analyse servie sous le seuil → pseudonymisation → validation humaine → rejeu au gate |

**« Dérive par rapport à quoi ? »** Par rapport à la confiance mesurée *par le
gate*, figée dans le manifeste de la version — pas une moyenne glissante, qui
absorberait lentement la dérive qu'elle est censée détecter.

### Journal de pilotage et seuils

| Fichier | Rôle |
|---|---|
| `ops/registry/journal.jsonl` | une ligne JSON par événement : date, signal, valeur, seuil, avant/après |
| [`ops/deploy.py::ajuster_seuil`](../ops/deploy.py) | tout changement de seuil est tracé avec auteur et motif |
| [`eval/anonymisation.py`](../eval/anonymisation.py) | pseudonymisation avant toute écriture disque |

Événements journalisés : `publication`, `canary`, `promotion_canary`,
`promotion_refusee`, `promotion`, `rollback`, `capture_faible_confiance`,
`enrichissement_jeu_evaluation`, `capture_rejetee`, `ajustement_seuil`.

---

## 5. Le client — frontend accessible par un lien

| Page | Fichier | Contenu |
|---|---|---|
| `/` | [`web/index.html`](../web/index.html) | le juriste colle un contrat, lit les clauses et leur **fiabilité** |
| `/pilotage` | [`web/tableau-de-bord.html`](../web/tableau-de-bord.html) | trafic, distribution, cas capturés, journal |
| — | [`app/frontend.py`](../app/frontend.py) | routes et API du frontend |
| — | [`app/frontend.py`](../app/frontend.py) | routes de pilotage, actives seulement si `DEMO=on` |
| — | [`render.yaml`](../render.yaml) | déploiement (Render, `MOCK=on` : la démonstration publique ne consomme pas de clé API) |

Le client tape sur `/analyse` (la gateway) et non sur `/v2` : il voit donc
exactement ce que voit un vrai client, canary compris. L'interface parle la
langue du métier — *fiable*, *à vérifier*, *à reprendre* — et non « score ».

---

## Documentation d'exploitation

**[`docs/exploitation.md`](../docs/exploitation.md)** — le document à ouvrir à
3 h du matin : qu'est-ce qu'une version, schéma d'étiquetage, chaîne de
livraison, déploiement progressif, procédure de rollback, seuils et leur
justification, faux positifs connus, et le transcript d'exécution complet des
trois boucles.

---

## Preuves d'exécution

```
29 tests verts    10 d'acceptance fournis (9 rouges au départ)
                  13 pour les critères restants (3 boucles, distribution, seuils, frontend)
                   6 d'intégration hérités de la remédiation
ruff check        propre
gate v2           note 1.000 · P95 21 ms · coût 0,0329 € · GATE : PASSE
gate v1           ÉCHEC sur c07, c10, c12 — la douleur du client, devenue mesure
CI                lint · tests · gate-evaluation · build  → verts

en ligne          POST /analyse sur un contrat de 40 pages
                  → 200, servi par v2.0.0, 35 sections, 35 appels LLM,
                    12 clauses relevées, fiabilité 0,734
                  → canary actif : v1.0.0 à 80 %, v2.0.0 à 20 %
```

### Rejouer la démonstration

```bash
make install
make test                                    # 29 tests
make eval VERSION=v1                         # la v1 échoue sur les contrats longs
make eval VERSION=v2                         # la v2 passe
make frontend                                # client + pilotage sur :8000

# les trois boucles
make traffic MODE=derive-score DUREE=120     # dérive injectée par le proxy
python -m ops.deploy surveiller --boucle     # boucle 1 : rollback automatique
python -m ops.deploy promouvoir-auto         # boucle 2 : promotion si conforme
make captures                                # boucle 3 : cas en attente de relecture
```

---

## Réserves

- Les mesures des transcripts viennent du mode `MOCK` : la confiance y est
  constante, donc **les seuils de dérive doivent être recalibrés sur du trafic
  réel** avant d'armer la surveillance automatique en production.
- Le passage du canary à 100 % reste une décision humaine : aucun signal
  disponible ne mesure la justesse juridique des clauses relevées.
- La traduction et le chatbot évoqués en fin d'expression de besoin sont hors
  périmètre, et pourquoi — voir le dossier de conception, §1.
