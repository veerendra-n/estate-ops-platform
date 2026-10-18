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
| Workflow | Postgres job queue (`FOR UPDATE SKIP LOCKED`) | Temporal |
| Events | Webhook straight into the API | NATS |
| Estate graph | Two Postgres tables and a recursive query | Neo4j, behind `estate.py` |
| Case ledger | Postgres, append-only, hash chained per customer | Daily export of the chain head to object storage |
| Policy | Python (`policy.py`) | Open Policy Agent if rules multiply per customer |
| Runbooks | ops-foundation, via Semaphore | — |

## Decisions

**1. One Postgres for the first slice, not Postgres plus Neo4j, NATS and Temporal.**
At reference-customer scale (about 300 servers, 50 apps, a few thousand graph edges per customer) a
recursive query answers neighbourhood questions in milliseconds, and a job table handles a few hundred
cases a day. Three extra services would each need running, patching, backing up and securing per platform
instance before they do anything Postgres can't. The seams are in place (`estate.py`, `jobs.py`,
the webhook) so each can be swapped without touching callers. Add them when:
- **Neo4j**: graph queries span customers' full estates or need path algorithms (blast radius across
  thousands of nodes), or queries pass about 100 ms.
- **Temporal**: workflows wait days (change windows, customer replies) or need timers and compensation.
- **NATS**: more than one consumer needs the same events (forecast agent, SLA clocks, the reporter).

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
