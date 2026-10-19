-- Operations data around the cases: the health of each connector and integration, and what changed in
-- each customer's estate graph (so agents can ask "what changed before it broke?").

CREATE TABLE integration_status (
    tenant_id     text NOT NULL REFERENCES tenants(id),
    name          text NOT NULL,
    kind          text NOT NULL CHECK (kind IN ('alerts', 'inventory', 'events')),
    ok            boolean NOT NULL,
    last_run_at   timestamptz NOT NULL DEFAULT now(),
    last_ok_at    timestamptz,
    last_error    text,
    detail        jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, name)
);

-- One row per asset a discovery run added, removed or changed (attributes before and after).
CREATE TABLE graph_changes (
    id          bigserial PRIMARY KEY,
    tenant_id   text NOT NULL REFERENCES tenants(id),
    at          timestamptz NOT NULL DEFAULT now(),
    source      text NOT NULL,
    asset_id    text NOT NULL,
    asset_name  text NOT NULL,
    change      text NOT NULL CHECK (change IN ('added', 'removed', 'changed')),
    before      jsonb,
    after       jsonb
);
CREATE INDEX graph_changes_tenant_at ON graph_changes (tenant_id, at DESC);
CREATE INDEX graph_changes_asset ON graph_changes (tenant_id, asset_id, at DESC);

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['integration_status', 'graph_changes']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (tenant_id = ANY (string_to_array('
            'current_setting(''app.tenants'', true), '',''))) WITH CHECK (tenant_id = ANY (string_to_array('
            'current_setting(''app.tenants'', true), '','')))', t);
    END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE ON integration_status TO estate_app;
GRANT SELECT, INSERT ON graph_changes TO estate_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO estate_app;
