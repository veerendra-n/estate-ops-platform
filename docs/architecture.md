# Architecture and decisions

The platform is the "brain" layer from the reference architecture: estate graph, agents, trust and policy,
and the console. Sensors (OpenObserve, collectors) and actuators (Semaphore running ops-foundation
runbooks) stay standard and swappable.

## Components today

| Layer | Now | Later, when it earns its place |
|---|---|---|
| Console | Plain HTML and JS served by the API | A richer app once there are several roles' views (the prototype has eight) |
| API | FastAPI | — |
| Agents | Diagnosis agent on the Claude API with typed, read-only tools | Concierge, forecast, problem and reporter agents on the same tool layer |
| Tools | Python functions with strict JSON schemas, bound to one customer | Published as an MCP server so the copilot and other runtimes share them |
| Workflow | Temporal, self-hosted (one `CaseWorkflow` per case, on Postgres) | Change windows, customer replies and compensation as more workflows |
| Events | NATS JetStream: intake queue and a case event stream | Consumers for Teams notices, SLA clocks, the forecast and reporter agents |
| Estate graph | Neo4j Community, behind `estate.py` | Discovery jobs filling it; path queries for blast radius |
| Case ledger | Postgres, append-only, hash chained per customer | Daily export of the chain head to object storage |
| Policy | Python (`policy.py`) | Open Policy Agent if rules multiply per customer |
| Runbooks | ops-foundation, via Semaphore | — |

All free, self-hosted editions: Neo4j Community (GPLv3), NATS (Apache 2.0), Temporal (MIT). Together they
add about 0.7 GB of memory to a platform instance at rest (measured in the local stack: Neo4j about
540 MB with a 512 MB heap cap, Temporal about 80 MB, NATS about 5 MB).

## How a case flows

```
OpenObserve ─webhook─▶ API: check token and payload ─▶ NATS <prefix>.intake.<tenant>  (Nats-Msg-Id = intake id)
                                                            │ durable "triage" consumer (worker)
                                       triage in Postgres: signal keyed by (intake id, row) ─▶ case
                                                            │ start CaseWorkflow "case-<id>" (idempotent)
Temporal CaseWorkflow:  diagnose ─▶ dry run (poll) ─▶ wait for "decide" update ─▶ live run (poll) ─▶ end
                          │ reads Neo4j            ▲ console approve/reject            │
                          ▼                        │ (validator refuses stale ones)    ▼
                     Postgres: case, proposal, ledger ─── relay ──▶ NATS <prefix>.events.<tenant>.<kind>
```

## Decisions

**1. Neo4j, NATS and Temporal from the first slice, all free editions.**
We chose to run them from the start rather than add them later behind seams. What each one carries now:
- **Temporal** holds every wait in a case: Semaphore polls, and an approval that may take hours (it
  escalates after `APPROVAL_TIMEOUT_HOURS`, 72 by default). A worker restart loses nothing, a failing step
  is retried and then escalated rather than left stuck, and the Temporal UI shows each case step by step.
  Approvals are a Workflow Update whose validator refuses a proposal that isn't waiting, so a double click
  or a stale console can't approve twice. Steps are written to be safe to run twice.
- **NATS JetStream** decouples intake from processing: the API answers OpenObserve at once and an alert
  storm queues instead of hitting Postgres. Publishing with the intake id as `Nats-Msg-Id` and keying
  signals by (intake id, row) means a retry or redelivery adds nothing. Every ledger entry is relayed to
  an events stream, so later consumers react to cases without polling the database.
- **Neo4j Community** holds the estate graph. Community has one database and no per-database access
  control, so isolation is in `estate.py`: every node carries `tenant_id`, every query matches on it, and
  agents only reach the graph through two fixed queries.

Postgres stays the system of record (cases, proposals, the ledger, AI usage) and also stores Temporal's
own data, so there is still one database to back up. Costs to watch: three more services to patch and
monitor per platform instance; Neo4j is the largest at about half a gigabyte.

**2. Claude API with our own tools, not the Claude Agent SDK, for operations agents.**
The Agent SDK is Claude Code as a library: a strong harness, but it brings shell and file tools. An agent
that touches customer estates must have no free shell and no way to reach another customer. Here every
tool is read-only, takes typed inputs, is bound to one customer's id and OpenObserve organisation, and
returns a summary rather than raw rows. Actions only happen through tested runbooks after policy and,
where required, a person. The Agent SDK is the right fit later for the skill-authoring agent that writes
new runbooks into ops-foundation as pull requests, because that work is coding.

**3. Deterministic first; the model is optional per customer.**
Most alerts map to exactly one runbook, and the runbook's own `decide.yml` checks the cause in the dry
run. The model adds the cross-system reasoning and the readable diagnosis. A customer can run
`diagnosis: deterministic` (no AI cost at all), and the model path falls back to the deterministic one on
any failure. Spend is metered per case; above the monthly budget only P1/P2 cases use the model.
Diagnosis runs at low effort with a cached system prompt and catalog, and at most four turns. The
expected cost per case is a few cents; the console shows the real figure per case and per customer.

**4. Always a dry run before an approval.**
The engineer approves what the runbook found on the real host (its `before` facts and decision), not what
the model guessed. If the runbook's checks decide to escalate or that nothing is needed, nobody is asked.

**5. Values from the alert beat values from the model.**
A model can add optional parameters (for example a shrink target) but cannot change the database or
host the alert named. Parameters are validated against `meta.yml`; run variables (`rb_tenant`,
`rb_targets`, `rb_dry_run`, `rb_approval_ref`) are set only by the platform.

**6. Isolation enforced by the database as well as the code.**
Every customer-owned table has row-level security keyed on a per-transaction setting, and the application
role owns no tables, so a query that forgets its filter still sees only its customer. Evidence lines are
only accepted for the customer and host the run was for.

**7. The ledger is the case conversation.**
There is no separate timeline table to drift from the audit trail. Agent posts, proposals, approvals and
evidence are entries in one append-only chain per customer; `GET /api/v1/tenants` reports whether each
chain verifies.

## Not in this slice

Sign-in is a shared console token plus the engineer's name on each approval; SSO (OIDC) comes before any
real customer. Teams approval cards, the HaloPSA note writer hook, discovery jobs that fill the estate
graph, the evaluation harness and the other agents are next. See [integrations.md](integrations.md) for
what still needs a lab check.
