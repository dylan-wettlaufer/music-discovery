#!/bin/sh
set -eu

attempts=0
until alembic upgrade head; do
  attempts=$((attempts + 1))
  if [ "$attempts" -ge 15 ]; then
    echo "database not ready; giving up" >&2
    exit 1
  fi
  echo "waiting for database ($attempts)..."
  sleep 2
done

exec "$@"
