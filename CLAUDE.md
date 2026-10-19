# Working in estate-ops-platform

- This repository stands alone. Do not depend on, copy from or mention ops-foundation, and do not add the
  interim tools (HaloPSA, Thread, NetBox, Semaphore, OpenObserve, Zabbix, Level, Teleport).
- Remediations are skills in `skills/<area>/<name>/` (skill.yml, skill.py with gather/decide/act/verify, and
  tests.yml). A skill acts only through the connectors it declares, and only after `policy/autonomy.rego`
  (and an approval where the skill requires it).
- Agents get read-only, typed tools bound to one tenant. No free shell, no free SQL, no Cypher, no writes.
- Every tenant-owned table has row-level security; use `Database.scope(tenant)` and add new tables to the
  RLS list in a migration. Never add a cross-tenant query outside the console's engineer scope.
- Every workflow step writes a ledger entry; the cockpit renders the ledger as the case conversation.
- `workflows.py` runs in Temporal's sandbox: standard library and temporalio only, activities by name.
  Steps in `service.py` must be safe to run twice (Temporal retries them) and return the next step.
- The estate graph is Neo4j Community with no per-database isolation: every node has `tenant_id` and every
  query in `estate.py` matches on it.
- Keep AI spend low: deterministic first, meter every model call with `budget.record`, keep the system
  prompt and tool list byte-stable so caching works.
- Integrations consume the NATS events stream (`integrations/__init__.py`); they must be idempotent per
  ledger entry and never block a case. New external ids go in `external_refs`.
- The estate simulator (`src/estate_sim`) is the only stand-in for customer systems. Keep its API shapes
  vendor-accurate and mark guesses UNCONFIRMED in `docs/integrations.md`.
- No secrets in the repo. `config/tenants.yml` names environment variables; `.env` is git-ignored.
- Before pushing: `ruff check . && ruff format --check . && python -m pytest` (needs Postgres, Neo4j, NATS
  with JetStream, a Temporal dev server and OPA; see README "Develop"), and in `cockpit/`
  `npm run typecheck && npm run build`.
