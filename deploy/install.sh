#!/usr/bin/env bash
# Install or update one Estate Ops platform instance on this VM (Ubuntu 24.04 LTS or Debian 12).
#
#   sudo DOMAIN=ops.example.net ACME_EMAIL=ops@example.net ./deploy/install.sh
#   sudo DOMAIN=ops.example.net OPS_FOUNDATION_DIR=/opt/ops-foundation ./deploy/install.sh   # with the real catalog
#
# Safe to run again: it keeps existing secrets and config, adds what is missing, rebuilds and restarts.
# Secrets are generated here and stay in deploy/.env (mode 600) on this VM; nothing secret is in the repo.
set -euo pipefail
cd "$(dirname "$0")"

DOMAIN="${DOMAIN:?set DOMAIN, the name engineers and OpenObserve use to reach this VM}"
ACME_EMAIL="${ACME_EMAIL:-ops@${DOMAIN}}"
say() { printf '\n== %s\n' "$*"; }

say "Docker"
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  if command -v apt-get >/dev/null; then
    apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q docker.io docker-compose-v2 openssl curl
    systemctl enable --now docker
  else
    echo "Install Docker with the compose plugin, then run this again." >&2; exit 1
  fi
fi
docker compose version

say "Config"
mkdir -p local/backups
chmod 700 local
[ -f local/tenants.yml ] || { cp ../config/tenants.yml local/tenants.yml; echo "copied config/tenants.yml to deploy/local/tenants.yml: edit it for your customers"; }
if [ -n "${OPS_FOUNDATION_DIR:-}" ]; then
  python3 "$OPS_FOUNDATION_DIR/tools/build_catalog.py"
  cp "$OPS_FOUNDATION_DIR/build/catalog.json" local/catalog.json
  echo "catalog from $OPS_FOUNDATION_DIR at $(git -C "$OPS_FOUNDATION_DIR" rev-parse --short HEAD 2>/dev/null || echo '?')"
elif [ ! -f local/catalog.json ]; then
  cp ../tests/fixtures/catalog.json local/catalog.json
  echo "WARNING: using the test snapshot of the runbook catalog; set OPS_FOUNDATION_DIR for the real one"
fi

say "Secrets (deploy/.env)"
touch .env && chmod 600 .env
set_default() {  # add KEY=value unless KEY is already set
  grep -q "^$1=" .env || printf '%s=%s\n' "$1" "$2" >> .env
}
rand() { openssl rand -hex 24; }
set_default DOMAIN "$DOMAIN"
set_default ACME_EMAIL "$ACME_EMAIL"
set_default POSTGRES_PASSWORD "$(rand)"
set_default NEO4J_PASSWORD "$(rand)"
set_default SESSION_SECRET "$(rand)"
set_default CONSOLE_TOKEN "$(rand)"   # only used until OIDC_ISSUER is set
while read -r var; do
  set_default "$var" "$(rand)"
done < <(grep -oE 'intake_token_env:[[:space:]]*[A-Z0-9_]+' local/tenants.yml | awk '{print $2}')
sed -i "s|^DOMAIN=.*|DOMAIN=$DOMAIN|" .env
echo "$(grep -c = .env) settings in deploy/.env"

say "Start"
[ -n "${SKIP_BUILD:-}" ] || docker compose build --pull   # SKIP_BUILD=1 uses an image already on this VM
docker compose up -d --remove-orphans

say "Health check"
curl_opts=(-fsS --max-time 5)
[ "$DOMAIN" = localhost ] && curl_opts+=(-k)
for _ in $(seq 1 60); do
  if curl "${curl_opts[@]}" "https://$DOMAIN/healthz" >/dev/null 2>&1; then
    curl "${curl_opts[@]}" "https://$DOMAIN/healthz"; echo
    break
  fi
  sleep 5
done || true
curl "${curl_opts[@]}" "https://$DOMAIN/healthz" >/dev/null || { echo "not healthy yet: docker compose ps; docker compose logs api" >&2; exit 1; }

say "Nightly backup"
if command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
  here="$(pwd)"
  cat > /etc/systemd/system/estate-ops-backup.service <<UNIT
[Unit]
Description=Estate Ops platform backup
[Service]
Type=oneshot
ExecStart=$here/backup.sh
UNIT
  cat > /etc/systemd/system/estate-ops-backup.timer <<UNIT
[Unit]
Description=Nightly Estate Ops platform backup
[Timer]
OnCalendar=*-*-* 02:30:00
RandomizedDelaySec=15m
Persistent=true
[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload && systemctl enable --now estate-ops-backup.timer
  systemctl list-timers estate-ops-backup.timer --no-pager | head -3
else
  echo "no systemd here: schedule $(pwd)/backup.sh yourself (for example from cron)"
fi

say "Done"
cat <<NEXT
Console:      https://$DOMAIN   (until SSO is set up, sign in with CONSOLE_TOKEN from deploy/.env)
Intake:       https://$DOMAIN/api/v1/intake/openobserve/<tenant>   (tokens in deploy/.env)
Temporal UI:  ssh -L 8233:localhost:8233 <this vm>, then http://localhost:8233
Next:
  1. deploy/local/tenants.yml: your customers and their HaloPSA, Teams, NetBox and Semaphore ids.
  2. deploy/.env: OIDC_*, HALO_*, NETBOX_*, SEMAPHORE_URL/TOKEN, OPENOBSERVE_*, ANTHROPIC_API_KEY.
  3. Run this script again, then: docker compose run --rm api provision all
NEXT
