"""The first slice end to end: OpenObserve alert -> case -> diagnosis -> dry run -> approval -> live run."""

from __future__ import annotations

import copy

from conftest import SQL_ALERT, drain
from fastapi.testclient import TestClient

from estate_ops import ledger
from estate_ops.api import create_app

LAB = {"Authorization": "Bearer lab-test-token"}
CONSOLE = {"Authorization": "Bearer console-test-token"}


def _api(platform) -> TestClient:
    return TestClient(create_app(platform))


def _case(api, case_id):
    resp = api.get(f"/api/v1/cases/{case_id}", headers=CONSOLE)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_alert_to_approved_fix(make_platform):
    platform = make_platform()
    api = _api(platform)

    resp = api.post("/api/v1/intake/openobserve/lab", json=SQL_ALERT, headers=LAB)
    assert resp.status_code == 202, resp.text
    case_id = resp.json()["cases"][0]["case_id"]
    assert resp.json()["cases"][0]["new"] is True

    assert drain(platform) == ["diagnose", "start_run", "poll_run", "poll_run"]
    detail = _case(api, case_id)
    assert detail["case"]["status"] == "awaiting_approval"
    [proposal] = detail["proposals"]
    assert proposal["runbook_id"] == "sql/log-backup-shrink"
    assert proposal["params"] == {"rb_database": "FinanceERP"}
    assert proposal["mode"] == "approve_to_run"
    assert proposal["dry_run_evidence"]["decision"]["action"] == "backup_and_shrink"
    assert proposal["dry_run_evidence"]["dry_run"] is True

    resp = api.post(f"/api/v1/proposals/{proposal['id']}/approve", json={"who": "Priya", "note": "ok"}, headers=CONSOLE)
    assert resp.status_code == 200, resp.text
    # A second approval of the same proposal is refused.
    again = api.post(f"/api/v1/proposals/{proposal['id']}/approve", json={"who": "Sam"}, headers=CONSOLE)
    assert again.status_code == 409

    drain(platform)
    detail = _case(api, case_id)
    assert detail["case"]["status"] == "resolved"
    assert detail["proposals"][0]["outcome"] == "fixed"
    assert detail["proposals"][0]["evidence"]["approval_ref"].endswith(proposal["id"])
    kinds = [e["kind"] for e in detail["timeline"]]
    assert kinds == [
        "case_opened",
        "diagnosis",
        "proposal",
        "run_started",
        "run_result",
        "approval_requested",
        "approved",
        "run_started",
        "run_result",
    ]
    assert detail["timeline"][1]["body"]["method"] == "deterministic"  # no model configured in this test
    with platform.db.scope("lab") as conn:
        assert ledger.verify(conn, "lab") == (True, None)


def test_repeat_alert_folds_into_open_case(make_platform):
    platform = make_platform()
    api = _api(platform)
    first = api.post("/api/v1/intake/openobserve/lab", json=SQL_ALERT, headers=LAB).json()["cases"][0]
    second = api.post("/api/v1/intake/openobserve/lab", json=SQL_ALERT, headers=LAB).json()["cases"][0]
    assert second["case_id"] == first["case_id"] and second["new"] is False
    assert drain(platform)[0] == "diagnose"
    detail = _case(api, first["case_id"])
    assert len(detail["signals"]) == 2
    assert [e["kind"] for e in detail["timeline"]].count("diagnosis") == 1


def test_intake_rejects_bad_token_and_foreign_rows(make_platform):
    api = _api(make_platform())
    assert api.post("/api/v1/intake/openobserve/lab", json=SQL_ALERT).status_code == 401
    assert (
        api.post(
            "/api/v1/intake/openobserve/lab", json=SQL_ALERT, headers={"Authorization": "Bearer contoso-test-token"}
        ).status_code
        == 401
    )
    assert api.post("/api/v1/intake/openobserve/nobody", json=SQL_ALERT, headers=LAB).status_code == 404
    foreign = copy.deepcopy(SQL_ALERT)
    foreign["rows"][0]["tenant_id"] = "contoso"
    assert api.post("/api/v1/intake/openobserve/lab", json=foreign, headers=LAB).status_code == 422
    assert api.get("/api/v1/cases").status_code == 401


def test_rows_as_json_lines_text(make_platform):
    platform = make_platform()
    api = _api(platform)
    payload = copy.deepcopy(SQL_ALERT)
    payload["rows"] = '{"tenant_id": "lab", "host_name": "lab-sql01", "sqlserver_database_name": "FinanceERP"}\n'
    resp = api.post("/api/v1/intake/openobserve/lab", json=payload, headers=LAB)
    assert resp.status_code == 202 and resp.json()["cases"][0]["target"] == "lab-sql01"


def test_unknown_alert_escalates_without_running_anything(make_platform):
    platform = make_platform()
    api = _api(platform)
    payload = {"alert_id": "cpu-steal-high", "rows": [{"tenant_id": "lab", "host_name": "lab-app01"}]}
    case_id = api.post("/api/v1/intake/openobserve/lab", json=payload, headers=LAB).json()["cases"][0]["case_id"]
    assert drain(platform) == ["diagnose"]
    detail = _case(api, case_id)
    assert detail["case"]["status"] == "escalated"
    assert detail["proposals"] == []


def test_target_outside_the_customer_estate_escalates(make_platform):
    platform = make_platform()
    api = _api(platform)
    payload = copy.deepcopy(SQL_ALERT)
    payload["rows"][0]["host_name"] = "cto-sql01"  # contoso's server, reported on lab's intake
    case_id = api.post("/api/v1/intake/openobserve/lab", json=payload, headers=LAB).json()["cases"][0]["case_id"]
    drain(platform)
    detail = _case(api, case_id)
    assert detail["case"]["status"] == "escalated"
    assert "not a known server" in detail["timeline"][-1]["body"]["reason"]


def test_runbook_checks_can_escalate_in_the_dry_run(make_platform, fakes_client):
    fakes_client.post(
        "/semaphore/_scenario/sql/log-backup-shrink",
        json={
            "before": {"log_reuse_wait": "ACTIVE_TRANSACTION", "oldest_open_tran_minutes": 95},
            "decision": {"action": "escalate", "reason": "An open transaction holds the log; a backup will not free it."},
            "actions": [],
            "after": {},
            "outcome": "escalated",
        },
    )
    platform = make_platform()
    api = _api(platform)
    case_id = api.post("/api/v1/intake/openobserve/lab", json=SQL_ALERT, headers=LAB).json()["cases"][0]["case_id"]
    drain(platform)
    detail = _case(api, case_id)
    assert detail["case"]["status"] == "escalated"
    assert "open transaction" in detail["timeline"][-1]["body"]["reason"]


def test_rejection_escalates(make_platform):
    platform = make_platform()
    api = _api(platform)
    case_id = api.post("/api/v1/intake/openobserve/lab", json=SQL_ALERT, headers=LAB).json()["cases"][0]["case_id"]
    drain(platform)
    pid = _case(api, case_id)["proposals"][0]["id"]
    assert (
        api.post(f"/api/v1/proposals/{pid}/reject", json={"who": "Priya", "note": "month end close"}, headers=CONSOLE).status_code
        == 200
    )
    assert drain(platform) == []
    detail = _case(api, case_id)
    assert detail["case"]["status"] == "escalated"
    assert detail["proposals"][0]["status"] == "rejected"


def test_run_and_notify_runs_after_a_clean_dry_run(make_platform):
    platform = make_platform()
    api = _api(platform)
    payload = {"alert_id": "print-spooler-stopped", "rows": [{"tenant_id": "contoso", "host_name": "cto-prt01"}]}
    resp = api.post("/api/v1/intake/openobserve/contoso", json=payload, headers={"Authorization": "Bearer contoso-test-token"})
    case_id = resp.json()["cases"][0]["case_id"]
    drain(platform)
    detail = _case(api, case_id)
    assert detail["case"]["status"] == "resolved"
    assert [e["kind"] for e in detail["timeline"]] == [
        "case_opened",
        "diagnosis",
        "proposal",
        "run_started",
        "run_result",
        "approved",
        "run_started",
        "run_result",
    ]
    assert detail["timeline"][5]["actor"] == "policy"


def test_guard_refusal_is_reported_as_failed(make_platform, fakes_client):
    """If an approval reference were ever lost, the runbook's guard refuses and the case escalates."""
    platform = make_platform()
    api = _api(platform)
    case_id = api.post("/api/v1/intake/openobserve/lab", json=SQL_ALERT, headers=LAB).json()["cases"][0]["case_id"]
    drain(platform)
    pid = _case(api, case_id)["proposals"][0]["id"]
    api.post(f"/api/v1/proposals/{pid}/approve", json={"who": "Priya"}, headers=CONSOLE)
    original = platform.semaphore.start

    def start_without_ref(project_id, runbook_id, extra_vars):
        extra_vars.pop("rb_approval_ref", None)
        return original(project_id, runbook_id, extra_vars)

    platform.semaphore.start = start_without_ref
    drain(platform)
    detail = _case(api, case_id)
    assert detail["case"]["status"] == "escalated"
    assert detail["proposals"][0]["outcome"] == "failed"
