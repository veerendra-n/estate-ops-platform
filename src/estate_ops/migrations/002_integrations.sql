-- Links from a case to its records in other systems (a Teams message, a vendor case), so each
-- integration stays idempotent when NATS redelivers an event.
CREATE TABLE external_refs (
    tenant_id   text NOT NULL REFERENCES tenants(id),
    case_id     uuid NOT NULL REFERENCES cases(id),
    system      text NOT NULL,
    ref         text NOT NULL,
    data        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, case_id, system)
);

-- Ledger entries an integration has already delivered (one row per integration and entry).
CREATE TABLE deliveries (
    tenant_id   text NOT NULL REFERENCES tenants(id),
    system      text NOT NULL,
    ledger_id   bigint NOT NULL,
    at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, system, ledger_id)
);

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['external_refs', 'deliveries']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (tenant_id = ANY (string_to_array('
            'current_setting(''app.tenants'', true), '',''))) WITH CHECK (tenant_id = ANY (string_to_array('
            'current_setting(''app.tenants'', true), '','')))', t);
    END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE ON external_refs TO estate_app;
GRANT SELECT, INSERT ON deliveries TO estate_app;
