"""Tenant isolation, the append-only ledger, intake authentication, alert parsing, policy, change tracking."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from conftest import TOKENS, alert, post_alerts

import estate_sim.app as sim
from estate_ops import discovery, ledger, sources, triage
from estate_ops.api import create_app
from estate_ops.policy import Policy, build_input
from estate_ops.skills import Skills
from estate_ops.tenancy import Tenant

FIRING = {"source": "test", "alert_id": "DatastoreLatencyHigh", "severity": "P2", "rows": [{"datastore": "CTO-DS-CRIT-02"}]}


def _ingest(platform, tenant_id, payload=FIRING, intake=None):
    with platform.db.scope(tenant_id) as conn:
        return triage.ingest(conn, platform.tenants.get(tenant_id), payload, intake or str(uuid.uuid4()), platform.graph)


def test_row_level_security_hides_other_customers(make_platform):
    platform = make_platform()
    _ingest(platform, "contoso")
    with platform.db.scope("fabrikam") as conn:
        assert conn.execute("SELECT count(*) AS n FROM cases").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM ledger").fetchone()["n"] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO cases (tenant_id, ref, title, severity, status, fingerprint) "
                "VALUES ('contoso', 'X-1', 'x', 'P3', 'open', 'f')"
            )
    with platform.db.unscoped() as conn:
        assert conn.execute("SELECT count(*) AS n FROM cases").fetchone()["n"] == 0
    with platform.db.scope(["contoso", "fabrikam"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM cases").fetchone()["n"] == 1


def test_estate_graph_is_scoped_to_one_customer(graph):
    assert graph.asset("contoso", "server:CTO-SHOP-SQL01") is not None
    assert graph.asset("fabrikam", "server:CTO-SHOP-SQL01") is None
    assert graph.neighbourhood("fabrikam", "server:CTO-SHOP-SQL01")["assets"] == []
    near = graph.neighbourhood("contoso", "datastore:CTO-DS-CRIT-02", hops=3)
    assert near["assets"] and not any(a["name"].startswith("FAB-") for a in near["assets"])
    assert not graph.related("fabrikam", ["datastore:CTO-DS-CRIT-02"], ["service:Checkout"])


def test_graph_knows_what_runs_where(graph):
    radius = graph.blast_radius("contoso", ["datastore:CTO-DS-CRIT-02"])
    assert {"Checkout", "Online shop", "Sales analytics"} <= set(radius["services"]) and radius["critical"]
    assert graph.blast_radius("fabrikam", ["backup_job:EHR SQL nightly"])["services"] == ["EHR"]
    assert graph.related("contoso", ["service:Checkout"], ["datastore:CTO-DS-CRIT-02"])  # service -> server -> datastore
    # Two test VMs share only a hypervisor host and a datastore far from the shop: not related to Checkout.
    assert not graph.related("contoso", ["server:CTO-TST200"], ["service:Checkout"])
    stats = graph.stats("contoso")
    assert stats["kinds"]["server"] == 300 and stats["kinds"]["endpoint"] == 3000
    assert set(stats["sources"]) == {"vcenter", "veeam", "intune", "m365", "network", "declared"}


def test_ledger_is_append_only_and_tamper_evident(make_platform, database_url):
    platform = make_platform()
    _ingest(platform, "contoso")
    with platform.db.scope("contoso") as conn:
        ledger.append(conn, "contoso", None, "test", "note", {"n": 1})
    with platform.db.scope("contoso") as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("UPDATE ledger SET body = '{}'")
    with psycopg.connect(database_url) as owner, pytest.raises(psycopg.errors.RaiseException):
        owner.execute("DELETE FROM ledger")
    # Even the owner, going around the trigger, is caught by the hash chain.
    with psycopg.connect(database_url, autocommit=True) as owner:
        owner.execute("ALTER TABLE ledger DISABLE TRIGGER ledger_no_update")
        owner.execute("UPDATE ledger SET body = jsonb_set(body, '{n}', '2') WHERE kind = 'note'")
    with platform.db.scope("contoso") as conn:
        ok, bad_id = ledger.verify(conn, "contoso")
        assert not ok and bad_id is not None


def test_triage_folds_repeats_and_related_alerts_and_keeps_others_apart(make_platform):
    platform = make_platform()
    first = _ingest(platform, "contoso", intake="i-1")[0]
    assert first.new
    assert _ingest(platform, "contoso", intake="i-1")[0].case_id == first.case_id  # redelivery
    again = _ingest(platform, "contoso")[0]  # the same alert firing again
    assert again.case_id == first.case_id and not again.new
    worse = {"source": "test", "alert_id": "ServiceLatencyHigh", "severity": "P1", "rows": [{"service": "Checkout"}]}
    related = _ingest(platform, "contoso", worse)[0]
    assert related.case_id == first.case_id
    unrelated = {"source": "test", "alert_id": "HostDiskFull", "severity": "P3", "rows": [{"host": "CTO-APP010", "mount": "C:"}]}
    assert _ingest(platform, "contoso", unrelated)[0].new
    with platform.db.scope("contoso") as conn:
        case = conn.execute("SELECT * FROM cases WHERE id = %s", (first.case_id,)).fetchone()
        n = conn.execute("SELECT count(*) AS n FROM signals WHERE case_id = %s", (first.case_id,)).fetchone()["n"]
    assert case["severity"] == "P1"  # raised by the worse alert, with the P1 clocks
    assert (case["resolve_due"] - case["opened_at"]).total_seconds() == 240 * 60
    assert case["assets"] == ["datastore:CTO-DS-CRIT-02", "service:Checkout"]
    assert n == 3
    cleared = dict(FIRING, event="cleared")
    assert _ingest(platform, "contoso", cleared)[0].case_id == first.case_id


def test_intake_refuses_bad_tokens_and_tenantless_alerts(make_platform):
    from fastapi.testclient import TestClient

    platform = make_platform()
    app = create_app(platform)
    with TestClient(app) as api:
        a = alert("DatastoreLatencyHigh", "contoso", {"datastore": "X"})
        assert post_alerts(api, [a], token="wrong").status_code == 401
        no_tenant = alert("DatastoreLatencyHigh", "contoso", {"datastore": "X"})
        del no_tenant["labels"]["tenant_id"]
        assert post_alerts(api, [no_tenant]).status_code == 422
        assert post_alerts(api, [alert("X", "nobody", {"host": "h"})]).status_code == 422
        assert post_alerts(api, {"not": "a list"}).status_code == 422
        resp = api.post("/api/v1/intake/generic/contoso", json=FIRING, headers={"Authorization": "Bearer fabrikam-test-token"})
        assert resp.status_code == 401
        resp = api.post(
            "/api/v1/intake/generic/contoso", json=FIRING, headers={"Authorization": f"Bearer {TOKENS['CONTOSO_INTAKE_TOKEN']}"}
        )
        assert resp.status_code == 202
        spoof = dict(FIRING, rows=[{"tenant_id": "fabrikam", "datastore": "X"}])
        resp = api.post(
            "/api/v1/intake/generic/contoso", json=spoof, headers={"Authorization": f"Bearer {TOKENS['CONTOSO_INTAKE_TOKEN']}"}
        )
        assert resp.status_code == 422
        assert api.get("/api/v1/cases").status_code == 401


def test_alertmanager_payloads_become_alerts():
    firing = alert("DatastoreLatencyHigh", "contoso", {"datastore": "CTO-DS-CRIT-02"}, value="38.2 ms", title="Datastore slow")
    [(tenant, intake_id, payload)] = sources.alertmanager([firing])
    assert tenant == "contoso" and payload["event"] == "firing" and payload["severity"] == "P2"
    assert payload["rows"] == [{"datastore": "CTO-DS-CRIT-02", "value": "38.2 ms"}] and payload["title"] == "Datastore slow"
    # vmalert re-sends a firing alert every evaluation: same intake id, so NATS and triage drop the repeats.
    assert sources.alertmanager([firing])[0][1] == intake_id
    [(_, cleared_id, cleared)] = sources.alertmanager(
        [alert("DatastoreLatencyHigh", "contoso", {"datastore": "CTO-DS-CRIT-02"}, ended=True)]
    )
    assert cleared["event"] == "cleared" and cleared_id != intake_id


def _tenant(**autonomy):
    return Tenant(id="t", name="T", intake_token_env="X", autonomy={"default": "run_and_notify", **autonomy})


def test_autonomy_policy_in_opa():
    from conftest import OPA_URL, ROOT

    skills = Skills.load(ROOT / "skills")
    policy = Policy(OPA_URL)
    quiet = {"dependents": 0, "services": [], "critical": False}
    never = {"live_runs_30d": 0, "failures_30d": 0}
    rebalance, restart = skills.get("vmware/rebalance-datastore"), skills.get("windows/restart-service")
    assert policy.decide(build_input(_tenant(), rebalance, ["d"], quiet, never)).mode == "approve_to_run"  # approval: required
    assert policy.decide(build_input(_tenant(), restart, ["h"], quiet, never)).mode == "run_and_notify"
    critical = {"dependents": 9, "services": ["Checkout"], "critical": True}
    assert policy.decide(build_input(_tenant(), restart, ["h"], critical, never)).mode == "run_and_notify"  # low risk
    assert policy.decide(build_input(_tenant(), restart, ["a", "b"], quiet, never)).mode == "recommend"  # max_targets 1
    failed = policy.decide(build_input(_tenant(), restart, ["h"], quiet, {"live_runs_30d": 3, "failures_30d": 1}))
    assert failed.mode == "approve_to_run" and "earn its autonomy back" in failed.reason
    assert (
        policy.decide(build_input(_tenant(**{"windows/restart-service": "recommend"}), restart, ["h"], quiet, never)).mode
        == "recommend"
    )
    assert Policy("http://127.0.0.1:9").decide({}).mode == "recommend"


def test_discovery_records_what_changed(make_platform, estate):
    platform = make_platform()
    tenant = platform.tenants.get("contoso")
    vcenter = next(c for name, _, c in discovery.collectors(platform.settings) if name == "vcenter")
    sim.ESTATES["contoso"].vms["CTO-BI-ETL01"].datastore = "CTO-DS-GEN-01"
    try:
        result = sources.discover(platform, tenant, "vcenter", vcenter)
        assert result["changes"] >= 1, result
        with platform.db.scope("contoso") as conn:
            row = conn.execute("SELECT * FROM graph_changes WHERE asset_id = 'server:CTO-BI-ETL01'").fetchone()
        assert (
            row["change"] == "changed"
            and row["before"] == {"datastore": "CTO-DS-CRIT-02"}
            and row["after"] == {"datastore": "CTO-DS-GEN-01"}
        )
        assert platform.graph.related("contoso", ["server:CTO-BI-ETL01"], ["datastore:CTO-DS-GEN-01"], 1)
    finally:
        estate.post("/_sim/reset")
        sources.discover(platform, tenant, "vcenter", vcenter)
    assert platform.graph.related("contoso", ["server:CTO-BI-ETL01"], ["datastore:CTO-DS-CRIT-02"], 1)


def test_symptoms_that_arrive_before_their_cause_still_fold(make_platform):
    # vmalert sends a batch in its own order: services sharing a datastore fold before the datastore alert arrives.
    platform = make_platform()
    shops = [
        {"source": "test", "alert_id": "ServiceLatencyHigh", "severity": "P2", "rows": [{"service": s}]}
        for s in ("Checkout", "Online shop")
    ]
    first, second = (_ingest(platform, "contoso", p)[0] for p in shops)
    assert second.new and second.case_id != first.case_id  # nothing links the two services yet
    cause = _ingest(platform, "contoso", FIRING)[0]
    assert cause.case_id == first.case_id and not cause.new  # the datastore under both: merged into the oldest
    with platform.db.scope("contoso") as conn:
        merged = conn.execute("SELECT status, parent_id FROM cases WHERE id = %s", (second.case_id,)).fetchone()
        n = conn.execute("SELECT count(*) AS n FROM signals WHERE case_id = %s", (first.case_id,)).fetchone()["n"]
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM ledger WHERE case_id = %s ORDER BY id", (first.case_id,))]
    assert merged["status"] == "merged" and str(merged["parent_id"]) == first.case_id
    assert n == 3 and "cases_merged" in kinds
