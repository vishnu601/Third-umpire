#!/bin/sh
# Boot: migrate, import fixtures (idempotent), serve. Runs on every start.
set -eu

python manage.py migrate --noinput
python manage.py seed

exec gunicorn config.wsgi:application \
  --bind "0.0.0.0:${PORT:-8080}" \
  --workers "${WEB_WORKERS:-2}" \
  --access-logfile -
