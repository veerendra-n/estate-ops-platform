# Working in estate-ops-platform

- Runbooks live only in ops-foundation. Never copy runbook code here; read the catalog and run through Semaphore.
- Agents get read-only, typed tools bound to one tenant. No free shell, no free SQL, no writes to an estate
  except through a runbook run that passed `policy.py` (and an approval where the runbook requires it).
- Every tenant-owned table has row-level security; use `Database.scope(tenant)` and add new tables to the
  RLS list in a migration. Never add a cross-tenant query outside the console's engineer scope.
- Every workflow step writes a ledger entry; the console renders the ledger as the case conversation.
- `workflows.py` runs in Temporal's sandbox: standard library and temporalio only, activities by name.
  Steps in `service.py` must be safe to run twice (Temporal retries them) and return the next step.
- The estate graph is Neo4j Community with no per-database isolation: every node has `tenant_id` and every
  query in `estate.py` matches on it. Agents never get a Cypher tool.
- Keep AI spend low: deterministic first, meter every model call with `budget.record`, keep the system
  prompt and tool list byte-stable so caching works.
- Integrations consume the NATS events stream (`integrations/__init__.py`); they must be idempotent per
  ledger entry and never block a case. New external ids go in `external_refs`.
- No secrets in the repo. `config/tenants.yml` names environment variables; `.env` is git-ignored.
- Before pushing: `ruff check . && ruff format --check . && python -m pytest` (needs Postgres, Neo4j, NATS
  with JetStream and a Temporal dev server; see README "Develop").
