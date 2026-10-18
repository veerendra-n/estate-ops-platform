# Working in estate-ops-platform

- Runbooks live only in ops-foundation. Never copy runbook code here; read the catalog and run through Semaphore.
- Agents get read-only, typed tools bound to one tenant. No free shell, no free SQL, no writes to an estate
  except through a runbook run that passed `policy.py` (and an approval where the runbook requires it).
- Every tenant-owned table has row-level security; use `Database.scope(tenant)` and add new tables to the
  RLS list in a migration. Never add a cross-tenant query outside the console's engineer scope.
- Every workflow step writes a ledger entry; the console renders the ledger as the case conversation.
- Keep AI spend low: deterministic first, meter every model call with `budget.record`, keep the system
  prompt and tool list byte-stable so caching works.
- No secrets in the repo. `config/tenants.yml` names environment variables; `.env` is git-ignored.
- Before pushing: `ruff check . && ruff format --check . && python -m pytest` (needs `TEST_DATABASE_URL`).
