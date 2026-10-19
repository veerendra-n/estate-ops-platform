# estate-ops-platform

An AI-native operations platform for a managed services team that runs IT for many mid-market customers.
Alerts from every customer arrive in one place, the platform folds them into cases using an estate graph,
an investigator agent finds the cause, and a tested **skill** fixes it under an autonomy policy: run it and
tell people, run it after an engineer approves, or only recommend it. Every step lands in a hash-chained
ledger that the cockpit shows as the case's conversation.

This repository is the platform only. It has no dependency on ops-foundation and uses no interim tools.

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/veerendra-n/estate-ops-platform?ref=claude/project-thread-5hpvr3)

## Try it in a Codespace

1. Open the link above and create the codespace (it asks for a 4-core, 16 GB machine).
   Optional: add a Codespaces secret `ANTHROPIC_API_KEY` first to turn on the model-backed investigator and
   the copilot. Without it the deterministic graph walk does the investigation, and both scenarios still work.
2. The codespace builds and starts the stack by itself (`scripts/demo.sh up`, a few minutes the first time).
   When the terminal prints the cockpit URL and the console token, open the **Ports** tab, open port 8080,
   and sign in with that token.
3. Give discovery about a minute to fill the estate graph for both customers (Overview shows the asset counts).

Ports are private to your GitHub account by default. Codespaces time on a private repository is billed to
the account that owns the codespace; stop it when you are done (`scripts/demo.sh down` keeps the data).

### Scenario 1: the checkout is slow (Contoso Retail, an engineer approves)

1. In the cockpit open **Lab**, pick **Contoso**, and start the fault **checkout-slow**. A BI reporting VM
   (CTO-BI-ETL01) starts a full reload on the datastore it shares with the shop's SQL server.
2. Within a minute or two the alert rules fire: datastore latency, SQL I/O waits, checkout latency and more.
   They end up as one case under **Cases**, not twenty: the slow services may open cases of their own for a
   moment, and those merge into one when the datastore under all of them alerts.
3. Open the case. The timeline shows the investigation (which asset, which evidence), the proposal
   `vmware/rebalance-datastore` with its blast radius and dry-run plan (move CTO-BI-ETL01 to a quiet,
   non-critical datastore), and the policy decision: approval needed, because the skill touches storage that
   critical services use.
4. Approve it. The worker runs the storage vMotion, then verifies that the VM moved, the source datastore
   recovered and the destination is not slow. The case resolves and checkout latency drops back under 400 ms.

### Scenario 2: the backup repository is full (Fabrikam Health, no person needed)

1. In **Lab**, pick **Fabrikam** and start **backup-repo-full**. The nightly EHR SQL backup fails because
   its repository has run out of space.
2. The Veeam poller turns the failed session into an alert and a case. The investigator picks
   `backup/free-space-and-rerun`, policy allows `run_and_notify` for Fabrikam, and the skill removes orphaned
   restore points, reruns the job and watches the new session until it succeeds.
3. The case resolves on its own; the timeline shows each step and who would have been told.

**Reset** in the Lab clears every fault. `scripts/demo.sh wipe` deletes all cases and data for a clean demo.

## What runs

| Layer | Component | Product |
| --- | --- | --- |
| Cockpit | Cases, approvals, estate map, skills, lab | React + TypeScript (Vite, React Flow), served by the API |
| API and intake | REST API, Alertmanager-compatible alert intake, generic webhooks | FastAPI, Python 3.13 |
| Case engine | One durable workflow per case (settle, investigate, dry run, decide, run, verify) | Temporal |
| Agents | Investigator (typed read-only tools, one tenant), copilot | Claude API, metered per call; graph walk fallback |
| Policy | Autonomy per skill and customer | Open Policy Agent (`policy/autonomy.rego`) |
| Skills | gather / decide / act / verify, with their own tests | `skills/` |
| Case store | Cases, signals, proposals, hash-chained ledger, row-level security per customer | PostgreSQL 16 |
| Estate graph | Assets and dependencies from discovery, change history | Neo4j Community |
| Events | Ledger events to integrations (Teams) | NATS JetStream |
| Telemetry | Metrics and logs per customer, alert rules | OpenTelemetry Collector, VictoriaMetrics, VictoriaLogs, vmalert |
| Customer systems | vCenter, Veeam, Microsoft Graph (Intune, M365), Fleet, edge node | Connectors in `src/estate_ops/connectors` |

In the codespace the two customers' systems are the **estate simulator** (`src/estate_sim`): it answers
with vendor-shaped APIs, exposes Prometheus metrics and ships logs over OTLP, and the Lab page injects
faults into it. See [docs/architecture.md](docs/architecture.md) and [docs/integrations.md](docs/integrations.md).

## Develop

```
scripts/dev-env.sh                 # writes .env with random local secrets (git-ignored)
scripts/demo.sh up | status | reset | wipe | down
```

Tests run against real Postgres, Neo4j, NATS (JetStream), a Temporal dev server and OPA; the simulator runs
in process. Start the servers, then:

```
docker run -d --name tpg -p 5432:5432 -e POSTGRES_HOST_AUTH_METHOD=trust postgres:16-alpine
docker run -d --name tneo -p 7687:7687 -e NEO4J_AUTH=neo4j/test-password neo4j:5.26.32-community
docker run -d --name tnats -p 4222:4222 nats:2.11-alpine -js
docker run -d --name ttemporal -p 7233:7233 temporalio/temporal:1.9.1 server start-dev --ip 0.0.0.0
docker run -d --name topa -p 8181:8181 -v "$PWD/policy:/policy:ro" openpolicyagent/opa:1.10.1 run --server --addr 0.0.0.0:8181 /policy/autonomy.rego

pip install -e '.[dev]'
ruff check . && ruff format --check . && python -m pytest
docker run --rm -v "$PWD/policy:/policy:ro" openpolicyagent/opa:1.10.1 test -v /policy
cd cockpit && npm ci && npm run typecheck && npm run build    # VITE_MOCK=1 npm run dev for the UI alone
```

Customers live in `config/tenants.yml`; it names environment variables for every secret and holds none.
`python -m estate_ops discover --tenant contoso` runs discovery once by hand.
