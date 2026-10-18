# Installing a platform instance

One VM runs the whole platform: Caddy (HTTPS), the API and console, the worker, Postgres, Neo4j
Community, NATS JetStream and Temporal, all from `deploy/compose.yaml`. Everything is free and
self-hosted, and the same steps work on VMware, Hyper-V, Azure, AWS or GCP.

## The VM

| | Minimum | Notes |
|---|---|---|
| OS | Ubuntu 24.04 LTS or Debian 12 | the installer adds Docker from the distribution |
| CPU / RAM | 2 vCPU, 4 GB | the stack uses about 1 GB at rest (measured: Neo4j 500 MB, Postgres 150 MB, API and worker 100 MB each) |
| Disk | 40 GB | images take about 4 GB; the rest is Postgres, backups and logs (capped at 100 MB per service) |
| Network in | TCP 443 and 80 | 80 is only for the Let's Encrypt challenge and the redirect to HTTPS |
| Network out | HaloPSA, Teams, NetBox, Semaphore, OpenObserve, the Claude API, Let's Encrypt | |
| DNS | a name such as `ops.example.net` pointing at the VM | OpenObserve and engineers use it |

## Install

```sh
git clone https://github.com/veerendra-n/estate-ops-platform /opt/estate-ops-platform
git clone https://github.com/veerendra-n/ops-foundation /opt/ops-foundation      # for the runbook catalog
cd /opt/estate-ops-platform
sudo DOMAIN=ops.example.net ACME_EMAIL=ops@example.net OPS_FOUNDATION_DIR=/opt/ops-foundation ./deploy/install.sh
```

The installer:
- adds Docker if it is missing;
- copies `config/tenants.yml` to `deploy/local/tenants.yml` and builds the catalog from ops-foundation;
- generates every secret into `deploy/.env` (mode 600): database and graph passwords, the session key, and
  one intake token per customer;
- builds and starts the stack, waits for `https://DOMAIN/healthz`, and schedules a nightly backup.

Running it again keeps the existing secrets and data, adds anything missing and restarts. That is also how
to upgrade: `git pull`, then run it again.

## Before real customers

1. **Sign-in.** Register an app in Entra ID (or Google) with the redirect URI `https://DOMAIN/auth/callback`,
   then set `OIDC_ISSUER`, `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET` in `deploy/.env`. After that the shared
   `CONSOLE_TOKEN` stops working. Don't expose the console on the internet before this is done.
2. **Customers.** In `deploy/local/tenants.yml`, give each customer its HaloPSA client and site, Teams webhook
   variable, NetBox tenant and Semaphore project. Put the webhook URLs in `deploy/.env`.
3. **Systems.** Set `HALO_*`, `NETBOX_*`, `SEMAPHORE_URL`/`SEMAPHORE_TOKEN`, `OPENOBSERVE_*` and, for model
   diagnosis, `ANTHROPIC_API_KEY` in `deploy/.env` (see `.env.example`), then run `install.sh` again.
4. **Provisioning.** `docker compose run --rm api provision all` (from `deploy/`) creates the Semaphore
   templates and the OpenObserve alerts that call this platform.

## Day to day

| Task | How |
|---|---|
| Status | `docker compose ps` and `curl https://DOMAIN/healthz` |
| Logs | `docker compose logs -f worker` (or `api`, `temporal`) |
| Temporal UI, Neo4j Browser | `ssh -L 8233:localhost:8233 -L 7474:localhost:7474 <vm>`, then http://localhost:8233 and :7474 |
| Back up now | `./deploy/backup.sh` (the timer runs it at 02:30 and keeps 14 days) |
| Off-site copy | set `RESTIC_REPOSITORY`, `RESTIC_PASSWORD` and the bucket credentials in `deploy/.env` |
| Restore | `./deploy/restore.sh deploy/local/backups/postgres-<time>.sql.gz` |
| Rebuild the graph | `docker compose run --rm api discover` |

The backup is Postgres only: cases, proposals, the ledger, AI usage and Temporal's own databases. The estate
graph is rebuilt from NetBox and the seed, and NATS only holds messages in flight. A restore was tested on
this stack: the cases came back and every customer's ledger chain verified.
