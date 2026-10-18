-- First schema: tenants, signals, cases, proposals, the action ledger and AI usage. The estate graph is in
-- Neo4j and the case workflow in Temporal.
-- Every tenant-owned table carries tenant_id and is protected by row-level security keyed on the
-- session setting app.tenants (a comma-separated list the API or worker sets per transaction).
-- The application connects as estate_app, which owns nothing and so cannot bypass the policies.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE tenants (
    id                    text PRIMARY KEY,
    name                  text NOT NULL,
    config                jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at            timestamptz NOT NULL DEFAULT now()
);

-- The estate graph lives in Neo4j (estate.py); cases refer to assets by their id.

CREATE SEQUENCE case_ref_seq;

CREATE TABLE cases (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    text NOT NULL REFERENCES tenants(id),
    ref          text NOT NULL,
    title        text NOT NULL,
    severity     text NOT NULL,
    status       text NOT NULL CHECK (status IN
                   ('open', 'diagnosing', 'awaiting_approval', 'running', 'resolved', 'escalated')),
    fingerprint  text NOT NULL,
    target       text,
    summary      text,
    opened_at    timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    resolved_at  timestamptz,
    UNIQUE (tenant_id, ref)
);
-- At most one open case per fingerprint: repeat alerts fold into it.
CREATE UNIQUE INDEX cases_one_open_per_fingerprint ON cases (tenant_id, fingerprint)
    WHERE status NOT IN ('resolved', 'escalated');

CREATE TABLE signals (
    id           bigserial PRIMARY KEY,
    tenant_id    text NOT NULL REFERENCES tenants(id),
    case_id      uuid REFERENCES cases(id),
    source       text NOT NULL,
    intake_id    text NOT NULL,
    row_no       integer NOT NULL,
    alert_id     text NOT NULL,
    fingerprint  text NOT NULL,
    target       text,
    labels       jsonb NOT NULL DEFAULT '{}'::jsonb,
    received_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX signals_case ON signals (case_id);
-- A redelivered intake message folds into nothing: each (intake, row) is stored once.
CREATE UNIQUE INDEX signals_once ON signals (tenant_id, intake_id, row_no);

CREATE TABLE proposals (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         text NOT NULL REFERENCES tenants(id),
    case_id           uuid NOT NULL REFERENCES cases(id),
    runbook_id        text NOT NULL,
    runbook_version   text NOT NULL,
    catalog_commit    text,
    targets           text NOT NULL,
    params            jsonb NOT NULL,
    risk              text NOT NULL,
    mode              text NOT NULL CHECK (mode IN ('recommend', 'approve_to_run', 'run_and_notify')),
    status            text NOT NULL CHECK (status IN
                        ('proposed', 'dry_running', 'awaiting_approval', 'approved', 'rejected',
                         'running', 'done', 'failed')),
    dry_run_evidence  jsonb,
    evidence          jsonb,
    outcome           text,
    decided_by        text,
    decided_at        timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX proposals_case ON proposals (case_id);

-- Append-only, hash-chained per tenant. The case conversation in the console is read from here.
CREATE TABLE ledger (
    id          bigserial PRIMARY KEY,
    tenant_id   text NOT NULL REFERENCES tenants(id),
    case_id     uuid REFERENCES cases(id),
    at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    actor       text NOT NULL,
    kind        text NOT NULL,
    body        jsonb NOT NULL,
    prev_hash   text NOT NULL,
    hash        text NOT NULL UNIQUE
);
CREATE INDEX ledger_case ON ledger (case_id, id);
CREATE INDEX ledger_tenant ON ledger (tenant_id, id);

CREATE FUNCTION ledger_is_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'ledger is append-only';
END $$;
CREATE TRIGGER ledger_no_update BEFORE UPDATE OR DELETE OR TRUNCATE ON ledger
    FOR EACH STATEMENT EXECUTE FUNCTION ledger_is_append_only();

CREATE TABLE ai_usage (
    id                  bigserial PRIMARY KEY,
    tenant_id           text NOT NULL REFERENCES tenants(id),
    case_id             uuid REFERENCES cases(id),
    agent               text NOT NULL,
    model               text NOT NULL,
    input_tokens        integer NOT NULL,
    output_tokens       integer NOT NULL,
    cache_read_tokens   integer NOT NULL DEFAULT 0,
    cache_write_tokens  integer NOT NULL DEFAULT 0,
    cost_usd            numeric(12, 6) NOT NULL,
    at                  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ai_usage_tenant_month ON ai_usage (tenant_id, at);

-- Where the NATS relay has got to in the ledger (one row per relay). Case events are published from
-- the ledger after commit, so a crash between commit and publish only delays an event, never loses it.
CREATE TABLE relay_cursor (
    name        text PRIMARY KEY,
    last_id     bigint NOT NULL DEFAULT 0,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['cases', 'signals', 'proposals', 'ledger', 'ai_usage']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (tenant_id = ANY (string_to_array('
            'current_setting(''app.tenants'', true), '',''))) WITH CHECK (tenant_id = ANY (string_to_array('
            'current_setting(''app.tenants'', true), '','')))', t);
    END LOOP;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'estate_app') THEN
        CREATE ROLE estate_app NOLOGIN;
    END IF;
END $$;
GRANT USAGE ON SCHEMA public TO estate_app;
GRANT SELECT ON tenants TO estate_app;
GRANT SELECT, INSERT, UPDATE ON cases, signals, proposals TO estate_app;
GRANT SELECT, INSERT, UPDATE ON relay_cursor TO estate_app;
GRANT SELECT, INSERT ON ledger, ai_usage TO estate_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO estate_app;
