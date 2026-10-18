# Integrations

| System | How the platform talks to it | State |
|---|---|---|
| OpenObserve alerts | Webhook destination → `POST /api/v1/intake/openobserve/{tenant}` with the tenant's Bearer token | Payload shape below; confirm in the lab |
| OpenObserve queries | `POST /api/{org}/_search` with SQL built from fixed templates (`telemetry.py`) | Faked locally; confirm stream and column names |
| ops-foundation catalog | Reads `build/catalog.json` (`CATALOG_FILE`); alert specs from its `alerts` key or `ALERTS_DIR` | Working; tests use a snapshot (`dev/refresh_catalog.py`) |
| Semaphore | REST API v2: list templates, start a task with extra variables, read status and output | Faked locally; confirm in the lab |
| Neo4j Community | Bolt driver; fixed queries per tenant (`estate.py`); filled by NetBox discovery and `config/seed/` | Working |
| NetBox | `GET /api/dcim/devices/`, `/api/virtualization/virtual-machines/`, `/clusters/` filtered by tenant slug, every `DISCOVERY_INTERVAL_MINUTES` (`integrations/netbox.py`, or `python -m estate_ops discover`) | Faked locally; field names from NetBox 4.7 serializers |
| HaloPSA | OAuth client credentials (`/auth/token`, scope `all`); `POST /api/Tickets` and `/api/Actions` (array bodies). One ticket per case, a private note per step, status set to closed on resolve (`integrations/halopsa.py`) | Faked locally; confirm the closed status id and ticket type on the instance |
| Microsoft Teams | Workflows webhook per customer channel; Adaptive Card 1.4 with `Action.OpenUrl` to the case (`integrations/teams.py`) | Faked locally; approve-in-Teams buttons need a bot (later) |
| SSO | OpenID Connect code flow with PKCE via Authlib; Entra ID or Google issuer (`auth.py`) | Checked end to end against a test identity provider; register the app in Entra or Google |
| Provisioning | `python -m estate_ops provision semaphore\|openobserve\|all`: Semaphore templates per runbook; OpenObserve template, destination and v2 alerts per alert spec (`integrations/provision.py`) | Faked locally; creates what is missing, never deletes |
| NATS JetStream | `<prefix>_INTAKE` (webhooks) and `<prefix>_EVENTS` (ledger entries) streams | Working |
| Temporal | `CaseWorkflow` on task queue `estate-cases`; approvals as the `decide` update | Working; self-hosted on Postgres |
| Claude API | `anthropic` SDK, diagnosis agent only | Mocked in tests; runs live with `ANTHROPIC_API_KEY` |

## OpenObserve webhook template

Configure a destination per customer organisation with the platform URL for that tenant and the header
`Authorization: Bearer <that tenant's intake token>`, and this body template:

```json
{"alert_id": "{alert_name}", "org": "{org_name}", "stream": "{stream_name}",
 "start": "{alert_start_time}", "end": "{alert_end_time}", "url": "{alert_url}", "rows": "{rows}"}
```

Alert names must equal the ops-foundation alert spec ids (for example `sql-log-used-high`), which is how
the platform finds the linked runbook and maps row columns to its parameters. The intake accepts `rows`
as a list of objects, a list of JSON strings, or newline-separated JSON text, because how OpenObserve
renders `{rows}` needs confirming.

## Semaphore conventions

- One project per customer; its id is `semaphore_project_id` in `config/tenants.yml`.
- One task template per runbook, **named exactly as the runbook id** (`sql/log-backup-shrink`),
  pointing at the catalog's `playbook` path in the ops-foundation repository, with the customer's
  inventory and the "allow extra variables override" option on.
- The platform starts tasks with `environment` set to a JSON object of extra variables:
  `rb_tenant`, `rb_targets`, `rb_dry_run`, `rb_run_id`, the runbook's parameters, and for live runs
  `rb_approval_ref`.
- It reads `RUNBOOK_EVIDENCE` lines from the task output and ignores any for another customer or host.

## Setting up a customer

1. `config/tenants.yml`: add `halopsa.client_id` (and `site_id`), `teams_webhook_env`, `netbox_tenant` and the
   `semaphore` inventory, repository and environment ids.
2. Put the secrets in the environment: the intake token, the Teams webhook URL, and once for the platform
   `HALO_*`, `NETBOX_*`, `OIDC_*`.
3. `python -m estate_ops provision all --tenant <id>` creates the Semaphore templates and the OpenObserve
   alerts that call this platform.
4. The worker discovers NetBox on its next run (or `python -m estate_ops discover`).

Integrations are delivered from the NATS events stream, one durable consumer each, in order and at least once.
If HaloPSA or Teams is down, events wait and are retried with backoff; a newly enabled integration starts
from new cases.

## To confirm in the lab

- Semaphore: `environment` is the field that carries extra variables on `POST /api/project/{id}/tasks`,
  and the API token scope needed to start tasks but not edit projects.
- OpenObserve: how `{rows}` renders in a webhook template; `histogram()` and `str_match_ignore_case()` in
  the search SQL; the `host_name` column on metrics and logs from the edge collector.
- HaloPSA: the closed status id (`GET /api/Status`), ticket type, and whether `team` takes a name or id.
- Teams: the Workflows trigger's authentication set to "Anyone" (the URL is the secret) and card rendering.
- OpenObserve: the v2 alerts API (`/api/v2/{org}/alerts`, folder `default`) on the deployed version.
- Semaphore: template creation fields (`app`, `playbook`, `inventory_id`, `repository_id`, `environment_id`).
- ops-foundation: adding the alert specs to `build/catalog.json` (an `alerts` key) so the platform needs
  only one file from that repo.
