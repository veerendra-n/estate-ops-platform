# Architecture

One platform serves many customers (tenants). Everything a customer owns carries `tenant_id`, and every
read path is bound to one tenant before it runs.

## Layers, top to bottom

| Layer | What it does | Product now | Later |
| --- | --- | --- | --- |
| Cockpit | Cases, approvals, copilot, estate map, skills, lab | React + TypeScript (Vite, React Flow) | Voice concierge (ElevenLabs) |
| API | REST for the cockpit, alert intake, generic webhooks, OIDC sign-in | FastAPI | Same |
| Case engine | One durable workflow per case | Temporal | Temporal Cloud if self-hosting gets heavy |
| Agents | Investigator, copilot | Claude API with typed tools | Architect and reporter agents |
| Policy | Autonomy per skill, customer and blast radius | Open Policy Agent | Same |
| Skills | Tested remediations: gather, decide, act, verify | Python in `skills/` | Skill library versioned per release |
| Case store | Cases, signals, proposals, ledger, AI spend | PostgreSQL 16 with row-level security | Managed Postgres |
| Estate graph | Assets, dependencies, change history | Neo4j Community | Same; per-tenant graphs if needed |
| Event bus | Ledger events to integrations | NATS JetStream | Same |
| Telemetry | Metrics, logs, alert rules | OTel Collector, VictoriaMetrics, VictoriaLogs, vmalert | Cluster versions of the same |
| Edge | Per-site collector and typed automation runner | Simulated in `src/estate_sim` | Edge node VM per customer site |

## A case, end to end

1. **Signals.** vmalert evaluates `alerts/estate.rules.yml` over every customer's metrics and posts to the
   Alertmanager-compatible intake; each alert's `tenant_id` label says whose it is. Pollers turn Veeam
   sessions and Microsoft 365 service health into alerts. Customers' own tools can post to
   `/api/v1/intake/{source}/{tenant}` with that customer's bearer token.
2. **Intake** publishes each alert to NATS with an id derived from its content, so redelivery and vmalert's
   re-sends are dropped.
3. **Triage** maps each alert to an asset (`<kind>:<name>`) and either folds it into an open case (same
   fingerprint, or an asset within two hops in the estate graph of one the case already holds, not through a
   shared hypervisor or switch) or opens a new case with the customer's SLA clocks.
4. **Workflow.** A new case starts a Temporal workflow. It waits a short settle period so related alerts
   join, then:
   - *investigate*: the investigator agent gets the case, the alerts and read-only tools (estate
     neighbourhood, named metric queries, log search, recent changes, past cases), all bound to the tenant.
     It answers in a fixed JSON schema: cause, evidence, skill and target. Without a model, over budget, or
     on any model failure, a deterministic graph walk does the same job: the lowest-layer alerting asset,
     and the first skill whose trigger fired on it.
   - *propose*: the platform validates the skill, the target and the parameters (alert labels beat the
     model's guesses), computes the blast radius from the graph and asks OPA for the mode.
   - *dry run*: the skill gathers facts and decides what it would do, without acting.
   - *decide*: `run_and_notify` runs now; `approve_to_run` waits for an engineer (timeout escalates);
     `recommend` escalates with the dry-run plan.
   - *run and verify*: the skill acts, then verifies against live data. Success resolves the case; a failed
     verification escalates and counts against the skill's autonomy for 30 days.
5. **Ledger.** Every step appends a hash-chained entry. The cockpit renders the ledger as the case's
   conversation, and integrations consume it from NATS (Teams cards for approvals, escalations and
   resolutions), idempotently per entry.

## Discovery and the estate graph

Collectors run per customer every 15 minutes, in order: vCenter (clusters, hosts, datastores, VMs and their
placement), Veeam (jobs, what they protect, repositories), Intune (devices and users), Microsoft 365
(services), the edge node (network devices) and the customer's declared business services
(`config/estate/<tenant>.yml`). Each run diffs against that source's last snapshot and writes the
differences to `graph_changes`, which the investigator reads as "recent changes".

Relations point from what depends to what it depends on: service `runs_on` server, server `stored_on`
datastore and `runs_on` hypervisor host, which is `member_of` a cluster; a backup job `protects` servers and
`writes_to` a repository.

## Safety rules

- Postgres row-level security on every tenant-owned table; the app connects as a role that cannot bypass it.
- Every Neo4j query matches on `tenant_id`; agents never get Cypher, SQL or a shell.
- Writes to an estate happen only inside a skill's `act`, after policy (and approval when required), and
  only through the connectors the skill declares.
- The ledger cannot be updated or deleted, and its hash chain catches tampering by the database owner.
- If OPA cannot be reached, every proposal falls back to `recommend`.
- No secrets in the repository: `config/tenants.yml` names environment variables.

## Keeping AI spend low

- Deterministic first: triage, correlation, blast radius, policy and skills use no model.
- One investigator call per case, with a byte-stable system prompt and tool list so prompt caching works,
  low effort, a turn limit and a monthly budget per customer (above it only P1 and P2 cases use the model).
- The copilot uses a small model. Every call is metered in `ai_usage` with its cost.
