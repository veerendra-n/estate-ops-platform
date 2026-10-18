"""Tenant isolation, the append-only ledger, the autonomy dial, catalog validation and evidence parsing."""

from __future__ import annotations

import psycopg
import pytest
from conftest import ROOT, SQL_ALERT

from estate_ops import ledger, policy, triage
from estate_ops.catalog import Catalog
from estate_ops.semaphore import parse_evidence
from estate_ops.tenancy import Tenant

CATALOG = Catalog.load(ROOT / "tests/fixtures/catalog.json")


def test_row_level_security_hides_other_customers(make_platform):
    platform = make_platform()
    with platform.db.scope("lab") as conn:
        triage.ingest(conn, "lab", CATALOG, SQL_ALERT)
    with platform.db.scope("contoso") as conn:
        assert conn.execute("SELECT count(*) AS n FROM cases").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM ledger").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM assets WHERE tenant_id = 'lab'").fetchone()["n"] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO cases (tenant_id, ref, title, severity, status, fingerprint) "
                "VALUES ('lab', 'X-1', 'x', 'P3', 'open', 'f')"
            )
    with platform.db.unscoped() as conn:
        assert conn.execute("SELECT count(*) AS n FROM cases").fetchone()["n"] == 0
    with platform.db.scope(["lab", "contoso"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM cases").fetchone()["n"] == 1


def test_ledger_is_append_only_and_tamper_evident(make_platform, database_url):
    platform = make_platform()
    with platform.db.scope("lab") as conn:
        triage.ingest(conn, "lab", CATALOG, SQL_ALERT)
        ledger.append(conn, "lab", None, "test", "note", {"n": 1})
    with platform.db.scope("lab") as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("UPDATE ledger SET body = '{}'")
    with psycopg.connect(database_url) as owner, pytest.raises(psycopg.errors.RaiseException):
        owner.execute("DELETE FROM ledger")
    # Even the owner, going around the trigger, is caught by the hash chain.
    with psycopg.connect(database_url, autocommit=True) as owner:
        owner.execute("ALTER TABLE ledger DISABLE TRIGGER ledger_no_update")
        owner.execute("UPDATE ledger SET body = jsonb_set(body, '{n}', '2') WHERE kind = 'note'")
    with platform.db.scope("lab") as conn:
        ok, bad_id = ledger.verify(conn, "lab")
        assert not ok and bad_id is not None


def _tenant(**autonomy):
    return Tenant(
        id="t",
        name="T",
        intake_token_env="X",
        openobserve_org="t",
        semaphore_project_id=1,
        autonomy={"default": "run_and_notify", **autonomy},
    )


def test_autonomy_dial_caps():
    rb = dict(CATALOG.runbook("sql/log-backup-shrink"))
    assert policy.decide(_tenant(), rb, ["h"]).mode == "approve_to_run"  # approval: required
    rb_low = dict(CATALOG.runbook("windows/restart-spooler"))
    assert policy.decide(_tenant(), rb_low, ["h"]).mode == "run_and_notify"
    assert policy.decide(_tenant(), rb_low, ["a", "b", "c", "d", "e", "f"]).mode == "recommend"
    assert policy.decide(_tenant(), dict(rb_low, risk="high"), ["h"]).mode == "recommend"
    assert policy.decide(_tenant(**{"windows/restart-spooler": "recommend"}), rb_low, ["h"]).mode == "recommend"


def test_catalog_param_validation():
    ok, errors = CATALOG.validate_params("sql/log-backup-shrink", {"rb_database": "FinanceERP", "rb_shrink": "false"})
    assert errors == [] and ok == {"rb_database": "FinanceERP", "rb_shrink": False}
    _, errors = CATALOG.validate_params("sql/log-backup-shrink", {})
    assert errors == ["rb_database is required by sql/log-backup-shrink"]
    _, errors = CATALOG.validate_params("sql/log-backup-shrink", {"rb_database": "x", "rb_tenant": "contoso", "rm": "-rf"})
    assert (
        "rb_tenant is set by the platform, not by a proposal" in errors
        and "rm is not a parameter of sql/log-backup-shrink" in errors
    )
    _, errors = CATALOG.validate_params("sql/log-backup-shrink", {"rb_database": "a\nb"})
    assert errors


def test_catalog_alert_links():
    assert CATALOG.runbooks_for_alert("sql-log-used-high") == ["sql/log-backup-shrink"]
    assert CATALOG.runbooks_for_alert("nothing") == []


@pytest.mark.parametrize(
    "line",
    [
        'RUNBOOK_EVIDENCE {"schema": "ops-foundation/evidence/v1", "outcome": "fixed"}',
        '    "msg": "RUNBOOK_EVIDENCE {\\"schema\\": \\"ops-foundation/evidence/v1\\", \\"outcome\\": \\"fixed\\"}"',
    ],
)
def test_evidence_parsing(line):
    assert parse_evidence(["noise", line]) == [{"schema": "ops-foundation/evidence/v1", "outcome": "fixed"}]


def test_evidence_with_other_schema_is_ignored():
    assert parse_evidence(['RUNBOOK_EVIDENCE {"schema": "something/else"}', "RUNBOOK_EVIDENCE {broken"]) == []
