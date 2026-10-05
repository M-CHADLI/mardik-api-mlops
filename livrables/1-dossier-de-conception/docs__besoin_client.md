# Expression de besoin — Mardik, direction juridique

*Note interne transmise à l'équipe IA — septembre 2026*

Bonjour,

Depuis six mois, l'outil d'analyse de contrats (« la v1 ») est utilisé
quotidiennement par nos trois juristes. Il nous fait gagner du temps sur les
contrats fournisseurs courts : on colle le texte, on obtient la liste des
clauses présentes, on sait où regarder. Le client interne (`client_v1`) est
intégré à notre outil de gestion documentaire et **il doit continuer de
fonctionner tel quel** pendant et après la mise à jour — nous n'avons pas de
budget pour le refaire.

## Ce qui ne va plus

1. **Les contrats longs.** Dès qu'un contrat dépasse une quinzaine de pages
   (contrats-cadres, appels d'offres, contrats de distribution : 30 à 40
   pages), l'outil « oublie » les clauses de la fin — précisément celles qui
   comptent : résiliation, pénalités, droit applicable. Nous avons cessé de
   lui confier ces contrats, qui sont pourtant ceux qui nous prennent le plus
   de temps.

2. **On ne sait pas quand relire.** L'outil répond toujours avec la même
   assurance. Nous voudrions un **score de confiance** — par clause et pour
   l'ensemble — pour savoir quand une relecture humaine s'impose et quand on
   peut faire confiance au résultat. Un score bas doit nous alerter ; un score
   haut doit être fiable.

## Nos contraintes

- **Délai de réponse** : moins de **8 secondes** pour 95 % des analyses
  (P95), y compris sur les contrats longs. Au-delà, les juristes reprennent
  la lecture à la main.
- **Coût** : moins de **0,15 € par analyse** en moyenne. Nous traitons
  environ 400 contrats par mois.
- **Aucune interruption** : la v1 doit rester disponible en permanence. Si la
  nouvelle version pose problème, nous voulons pouvoir **revenir à la
  précédente immédiatement**, sans attendre un correctif.
- Nous voulons pouvoir **constater** que la nouvelle version est meilleure
  avant qu'elle ne soit généralisée, et **suivre** son comportement une fois
  en production (temps de réponse, erreurs, confiance).

## Et pendant qu'on y est

On aimerait aussi que l'outil **traduise** les contrats en anglais quand nos
partenaires étrangers nous les demandent, et — c'est une demande de la
direction générale — un **chatbot** avec lequel les juristes pourraient
discuter du contrat. Si c'est possible dans la même mise à jour, tant mieux.

Merci d'avance,

*Responsable juridique, Mardik*
