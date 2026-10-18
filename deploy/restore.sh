#!/usr/bin/env bash
# Restore Postgres from a backup written by backup.sh, then rebuild the estate graph.
#   ./deploy/restore.sh deploy/local/backups/postgres-20261118T023000Z.sql.gz
set -euo pipefail
cd "$(dirname "$0")"
file="$(realpath "${1:?give the backup file}")"
read -r -p "This replaces all platform data with $file. Type restore to go on: " answer
[ "$answer" = restore ] || exit 1
docker compose stop caddy api worker temporal temporal-ui
docker compose up -d postgres
until docker compose exec -T postgres pg_isready -U estate >/dev/null; do sleep 2; done
gunzip -c "$file" | docker compose exec -T postgres psql -q -U estate -d postgres -v ON_ERROR_STOP=0 >/dev/null
docker compose up -d
docker compose run --rm api discover || true   # the graph comes back from NetBox
echo "restored from $file"
