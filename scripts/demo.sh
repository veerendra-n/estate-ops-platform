#!/usr/bin/env bash
# The demo stack: up, status, reset, down.
#   scripts/demo.sh up      build and start everything, wait for the cockpit, print how to sign in
#   scripts/demo.sh status  what is running
#   scripts/demo.sh reset   clear every fault in the simulated estates (cases stay, as history)
#   scripts/demo.sh wipe    stop and delete all data (cases, graph, metrics) for a clean demo
#   scripts/demo.sh down    stop, keep the data
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || scripts/dev-env.sh

url() {
  if [ -n "${CODESPACE_NAME:-}" ]; then
    echo "https://${CODESPACE_NAME}-8080.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
  else
    echo "http://localhost:8080"
  fi
}

case "${1:-up}" in
  up)
    docker compose up -d --build
    echo "waiting for the cockpit..."
    for _ in $(seq 1 120); do
      if curl -fsS http://localhost:8080/healthz >/dev/null 2>&1; then break; fi
      sleep 3
    done
    curl -fsS http://localhost:8080/healthz >/dev/null || { echo "the API did not come up; see: docker compose logs api"; exit 1; }
    echo
    echo "Cockpit:     $(url)"
    echo "Sign in with the console token: $(grep '^CONSOLE_TOKEN=' .env | cut -d= -f2)"
    echo "The first discovery of both estates takes about a minute after start."
    ;;
  status) docker compose ps ;;
  reset) curl -fsS -X POST http://localhost:8095/_sim/reset 2>/dev/null || docker compose exec -T sim python -c \
      "import urllib.request; urllib.request.urlopen(urllib.request.Request('http://localhost:8095/_sim/reset', method='POST'))"; echo "faults cleared" ;;
  wipe) docker compose down -v ;;
  down) docker compose down ;;
  *) echo "usage: $0 {up|status|reset|wipe|down}"; exit 2 ;;
esac
