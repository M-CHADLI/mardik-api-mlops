#!/bin/sh
# Démarrage en hébergement : amorçage du registre puis service.
#
# Le port vient de l'hébergeur ($PORT), d'où ce script plutôt qu'un CMD figé.
# L'amorçage ne doit jamais empêcher le service de démarrer : s'il échoue, on
# sert quand même, et /gateway/etat dira qu'aucune version n'est active.
set -e
python -m scripts.amorcer_demo || echo "amorçage ignoré (registre déjà présent ou gate en échec)" >&2
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
