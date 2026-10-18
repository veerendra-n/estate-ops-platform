# estate-ops-platform

The agentic layer of our managed operations service. It turns a monitoring alert into a diagnosed case,
proposes a tested runbook from [ops-foundation](https://github.com/veerendra-n/ops-foundation), dry runs it,
asks an engineer to approve, runs it through Semaphore and records every step in a tamper-evident ledger.

ops-foundation holds *how* (runbooks, edge node, telemetry). This repo decides *what* and *when*.
Runbooks are never copied here: the platform reads ops-foundation's `build/catalog.json` and asks
Semaphore to run them.

## The first slice

```
OpenObserve alert ──webhook──▶ API ──▶ NATS JetStream ──▶ triage (no model) ──▶ case ──▶ Temporal CaseWorkflow
                                                                                      │
            estate graph (Neo4j) · metrics · logs ◀── tools ── diagnosis agent (Claude, or deterministic)
                                                                                      │
                                                     proposal ◀── catalog check + autonomy policy
                                                                                      │
                                                Semaphore dry run ──▶ RUNBOOK_EVIDENCE ──▶ approval card
                                                                                      │ engineer approves
                                                Semaphore live run ─▶ RUNBOOK_EVIDENCE ──▶ resolved / escalated
```

- **Intake** (`POST /api/v1/intake/openobserve/{tenant}`): per-customer bearer token; rows stamped with
  another customer's `tenant_id` are refused. Accepted alerts queue on NATS JetStream and the worker
  triages them; a redelivered alert adds nothing, and repeat firings fold into the one open case.
- **Workflow**: each case is a Temporal workflow. It holds the waits (runs, approvals), retries failed steps,
  escalates a case whose step keeps failing or whose approval nobody answers, and shows in the Temporal UI.
- **Diagnosis**: Claude with three read-only tools bound to one customer (estate graph, a metric series,
  log lines), a fixed answer schema, a few turns at most and low effort. It may only pick a runbook from
  the catalog. Without an API key, over budget, on refusal or on a bad answer it falls back to the
  alert's own runbook link. Customers can be set to deterministic only (no model calls at all).
- **Proposal**: parameters are validated against the runbook's `meta.yml`; values from the alert row
  always beat the model's; the target must be a server in that customer's estate.
- **Autonomy dial** (`policy.py`): `recommend`, `approve_to_run` or `run_and_notify` per customer and
  runbook. Approval-required runbooks never run without a person; high-risk runbooks are never run by agents.
- **Runs**: always a dry run first. The approval reference (`case/proposal`) goes to the runbook's own
  guard, which refuses a live run without it.
- **Ledger**: append-only, hash chained per customer; the case conversation in the console *is* the ledger.
  Every entry is also published to NATS (`<prefix>.events.<tenant>.<kind>`) for other consumers.
- **Integrations**: HaloPSA ticket per case with a note per step, closed on resolve; Teams cards when a
  case needs a person or was fixed; NetBox discovery into the estate graph; Semaphore templates and
  OpenObserve alerts provisioned from the ops-foundation catalog; console sign-in with Entra ID or Google.
- **AI spend**: every model call is metered per customer and case; above the monthly budget only P1/P2
  cases use the model.

## Run it locally

Needs Docker. Semaphore, OpenObserve, HaloPSA, Teams and NetBox are replaced by fakes (`dev/fakes.py`) that behave like
ops-foundation's runbooks on the fictitious `lab` customer.

```sh
cp .env.example .env              # placeholders only; add ANTHROPIC_API_KEY to use Claude
docker compose up -d --build
python dev/fire_alert.py          # SQL log almost full on lab-sql01
open http://localhost:8080        # sign in with any name and the CONSOLE_TOKEN from .env
```

Tickets and Teams cards from the fake HaloPSA and Teams show at http://localhost:8090/_inbox.
The stack also runs Postgres, Neo4j Community (browser on http://localhost:7474), NATS JetStream and
Temporal (UI on http://localhost:8233, one workflow per case).

The case appears under *Needs attention* with a dry run ready to approve. Approve it and it resolves.
`python dev/fire_alert.py print-spooler-stopped lab-prt01` shows a low-risk runbook; an unknown alert
id shows an escalation.

## Develop

```sh
python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
docker run -d --name t-pg -p 5432:5432 -e POSTGRES_HOST_AUTH_METHOD=trust postgres:16-alpine
docker run -d --name t-neo4j -p 7687:7687 -e NEO4J_AUTH=neo4j/test-password neo4j:5.26.32-community
docker run -d --name t-nats -p 4222:4222 nats:2.11-alpine -js
docker run -d --name t-temporal -p 7233:7233 temporalio/temporal:1.9.1 server start-dev --ip 0.0.0.0
python -m pytest
ruff check . && ruff format --check .
```

Tests use real servers because row-level security, the ledger trigger, graph isolation, redelivery and the
workflow are part of what they test. The defaults match the commands above; `TEST_DATABASE_URL`,
`TEST_NEO4J_URL`, `TEST_NEO4J_PASSWORD`, `TEST_NATS_URL` and `TEST_TEMPORAL_ADDRESS` point elsewhere.

## Layout

| Path | What it holds |
|---|---|
| `src/estate_ops/triage.py` | Alert checks, fingerprinting, folding repeats into one case |
| `src/estate_ops/workflows.py` | The Temporal case workflow: order of steps, waits, the approval update |
| `src/estate_ops/worker.py` | Temporal activities, the NATS triage consumer, the ledger relay |
| `src/estate_ops/bus.py` | NATS JetStream streams and subjects |
| `src/estate_ops/integrations/` | HaloPSA, Teams, NetBox discovery, provisioning of Semaphore and OpenObserve |
| `src/estate_ops/auth.py` | Console sign-in with OpenID Connect |
| `src/estate_ops/agents/diagnosis.py` | The diagnosis agent: tools, answer schema, fallbacks |
| `src/estate_ops/service.py` | The case steps: diagnose, propose, dry run, approve, run |
| `src/estate_ops/policy.py` | The autonomy dial |
| `src/estate_ops/catalog.py` | Reads ops-foundation's catalog and alert specs; validates parameters |
| `src/estate_ops/semaphore.py`, `telemetry.py` | Semaphore runs and OpenObserve queries |
| `src/estate_ops/ledger.py`, `budget.py`, `estate.py` | Ledger, AI spend, estate graph (Neo4j) |
| `src/estate_ops/migrations/` | Schema, including row-level security per customer |
| `console/` | The engineer console (plain HTML and JS, served by the API) |
| `config/` | Customers and the lab estate seed. No secrets: tokens are named by environment variable |
| `dev/` | Fakes, a sample alert sender, the catalog refresher |
| `docs/` | [Architecture and decisions](docs/architecture.md), [integrations to confirm](docs/integrations.md) |

## Secrets

None in this repo. Tokens and keys come from the environment (OpenBao in production, `.env` locally,
which git ignores). `config/tenants.yml` only names the variables.
