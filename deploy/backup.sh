#!/usr/bin/env bash
# Back up the platform's Postgres (cases, ledger, AI usage, and Temporal's own databases) to
# deploy/local/backups, keep KEEP_DAYS days, and copy off the VM with restic when RESTIC_REPOSITORY is set
# in deploy/.env (any S3-compatible bucket, Azure Blob, GCS or SFTP). The estate graph is rebuilt from
# NetBox and the seed, and NATS only holds messages in flight, so neither needs a backup.
set -euo pipefail
cd "$(dirname "$0")"
KEEP_DAYS="${KEEP_DAYS:-14}"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
out="local/backups/postgres-$ts.sql.gz"
docker compose exec -T postgres pg_dumpall -U estate --clean --if-exists | gzip > "$out.part"
mv "$out.part" "$out"
echo "wrote $out ($(du -h "$out" | cut -f1))"
find local/backups -name 'postgres-*.sql.gz' -mtime +"$KEEP_DAYS" -delete
if grep -q '^RESTIC_REPOSITORY=' .env; then
  docker run --rm --env-file .env -v "$(pwd)/local/backups:/backups:ro" restic/restic:0.18.0 \
    backup --host estate-ops --tag postgres /backups
  docker run --rm --env-file .env restic/restic:0.18.0 forget --host estate-ops --keep-daily 14 --keep-weekly 8 --prune
fi
