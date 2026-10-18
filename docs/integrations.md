# Integrations

| System | How the platform talks to it | State |
|---|---|---|
| OpenObserve alerts | Webhook destination → `POST /api/v1/intake/openobserve/{tenant}` with the tenant's Bearer token | Payload shape below; confirm in the lab |
| OpenObserve queries | `POST /api/{org}/_search` with SQL built from fixed templates (`telemetry.py`) | Faked locally; confirm stream and column names |
| ops-foundation catalog | Reads `build/catalog.json` (`CATALOG_FILE`); alert specs from its `alerts` key or `ALERTS_DIR` | Working; tests use a snapshot (`dev/refresh_catalog.py`) |
| Semaphore | REST API v2: list templates, start a task with extra variables, read status and output | Faked locally; confirm in the lab |
| Neo4j Community | Bolt driver; two fixed queries per tenant (`estate.py`); seeded from `config/seed/` | Working; discovery jobs to fill it are next |
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

## To confirm in the lab

- Semaphore: `environment` is the field that carries extra variables on `POST /api/project/{id}/tasks`,
  and the API token scope needed to start tasks but not edit projects.
- OpenObserve: how `{rows}` renders in a webhook template; `histogram()` and `str_match_ignore_case()` in
  the search SQL; the `host_name` column on metrics and logs from the edge collector.
- ops-foundation: adding the alert specs to `build/catalog.json` (an `alerts` key) so the platform needs
  only one file from that repo.
