# estate-ops-platform

The agentic layer of our managed operations service. It turns a monitoring alert into a diagnosed case,
proposes a tested runbook from [ops-foundation](https://github.com/veerendra-n/ops-foundation), dry runs it,
asks an engineer to approve, runs it through Semaphore and records every step in a tamper-evident ledger.

ops-foundation holds *how* (runbooks, edge node, telemetry). This repo decides *what* and *when*.
Runbooks are never copied here: the platform reads ops-foundation's `build/catalog.json` and asks
Semaphore to run them.

## The first slice

```
OpenObserve alert ──webhook──▶ intake ─▶ triage (no model) ─▶ case
                                                          │
            estate graph · metrics · logs ◀── tools ── diagnosis agent (Claude, or deterministic)
                                                          │
                                         proposal ◀── catalog check + autonomy policy
                                                          │
                                    Semaphore dry run ──▶ RUNBOOK_EVIDENCE ──▶ approval card
                                                          │ engineer approves
                                    Semaphore live run ─▶ RUNBOOK_EVIDENCE ──▶ resolved / escalated
```

- **Intake** (`POST /api/v1/intake/openobserve/{tenant}`): per-customer bearer token; rows stamped with
  another customer's `tenant_id` are refused. Repeat firings fold into the one open case.
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
- **AI spend**: every model call is metered per customer and case; above the monthly budget only P1/P2
  cases use the model.

## Run it locally

Needs Docker. Semaphore and OpenObserve are replaced by fakes (`dev/fakes.py`) that behave like
ops-foundation's runbooks on the fictitious `lab` customer.

```sh
cp .env.example .env              # placeholders only; add ANTHROPIC_API_KEY to use Claude
docker compose up -d --build
python dev/fire_alert.py          # SQL log almost full on lab-sql01
open http://localhost:8080        # sign in with any name and the CONSOLE_TOKEN from .env
```

The case appears under *Needs attention* with a dry run ready to approve. Approve it and it resolves.
`python dev/fire_alert.py print-spooler-stopped lab-prt01` shows a low-risk runbook; an unknown alert
id shows an escalation.

## Develop

```sh
python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
export TEST_DATABASE_URL=postgresql://postgres@localhost:5432/postgres   # any Postgres 16 you can create databases on
python -m pytest
ruff check . && ruff format --check .
```

Tests use a real Postgres because row-level security and the ledger trigger are part of what they test.

## Layout

| Path | What it holds |
|---|---|
| `src/estate_ops/triage.py` | Alert intake, fingerprinting, folding repeats into one case |
| `src/estate_ops/agents/diagnosis.py` | The diagnosis agent: tools, answer schema, fallbacks |
| `src/estate_ops/service.py` | The case workflow: diagnose, propose, dry run, approve, run |
| `src/estate_ops/policy.py` | The autonomy dial |
| `src/estate_ops/catalog.py` | Reads ops-foundation's catalog and alert specs; validates parameters |
| `src/estate_ops/semaphore.py`, `telemetry.py` | Semaphore runs and OpenObserve queries |
| `src/estate_ops/ledger.py`, `budget.py`, `estate.py` | Ledger, AI spend, estate graph |
| `src/estate_ops/migrations/` | Schema, including row-level security per customer |
| `console/` | The engineer console (plain HTML and JS, served by the API) |
| `config/` | Customers and the lab estate seed. No secrets: tokens are named by environment variable |
| `dev/` | Fakes, a sample alert sender, the catalog refresher |
| `docs/` | [Architecture and decisions](docs/architecture.md), [integrations to confirm](docs/integrations.md) |

## Secrets

None in this repo. Tokens and keys come from the environment (OpenBao in production, `.env` locally,
which git ignores). `config/tenants.yml` only names the variables.
