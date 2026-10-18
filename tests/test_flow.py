"""The first slice end to end: OpenObserve alert -> NATS -> case -> Temporal workflow -> dry run -> approval -> live run."""

from __future__ import annotations

import copy

from conftest import SQL_ALERT, wait_for

from estate_ops import ledger, triage

LAB = {"Authorization": "Bearer lab-test-token"}
CONTOSO = {"Authorization": "Bearer contoso-test-token"}
CONSOLE = {"Authorization": "Bearer console-test-token"}
SETTLED = ("awaiting_approval", "resolved", "escalated")


def post(api, payload, tenant="lab", headers=LAB) -> str:
    resp = api.post(f"/api/v1/intake/openobserve/{tenant}", json=payload, headers=headers)
    assert resp.status_code == 202, resp.text
    return resp.json()["intake_id"]


def detail(api, case_id):
    resp = api.get(f"/api/v1/cases/{case_id}", headers=CONSOLE)
    assert resp.status_code == 200, resp.text
    return resp.json()


def settled_case(api, statuses=SETTLED, last_kind: str | None = None):
    """The only case, once it reaches one of statuses (and, if given, its ledger ends with last_kind)."""

    def check():
        cases = api.get("/api/v1/cases", headers=CONSOLE).json()
        if len(cases) == 1 and cases[0]["status"] in statuses:
            d = detail(api, cases[0]["id"])
            if last_kind is None or (d["timeline"] and d["timeline"][-1]["kind"] == last_kind):
                return d
        return None

    return wait_for(check, what=f"a case in {statuses}")


def kinds(d) -> list[str]:
    return [e["kind"] for e in d["timeline"]]


def test_alert_to_approved_fix(make_stack):
    stack = make_stack()
    api = stack.api
    post(api, SQL_ALERT)

    d = settled_case(api, last_kind="approval_requested")
    assert d["case"]["status"] == "awaiting_approval"
    [proposal] = d["proposals"]
    assert proposal["runbook_id"] == "sql/log-backup-shrink"
    assert proposal["params"] == {"rb_database": "FinanceERP"}
    assert proposal["mode"] == "approve_to_run"
    assert proposal["dry_run_evidence"]["decision"]["action"] == "backup_and_shrink"
    assert proposal["dry_run_evidence"]["dry_run"] is True

    resp = api.post(f"/api/v1/proposals/{proposal['id']}/approve", json={"who": "Priya", "note": "ok"}, headers=CONSOLE)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    # A second approval of the same proposal is refused.
    again = api.post(f"/api/v1/proposals/{proposal['id']}/approve", json={"who": "Sam"}, headers=CONSOLE)
    assert again.status_code == 409

    d = settled_case(api, ("resolved", "escalated"))
    assert d["case"]["status"] == "resolved"
    assert d["proposals"][0]["outcome"] == "fixed"
    assert d["proposals"][0]["evidence"]["approval_ref"].endswith(proposal["id"])
    assert kinds(d) == [
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
    assert d["timeline"][1]["body"]["method"] == "deterministic"  # no model configured in this test
    with stack.platform.db.scope("lab") as conn:
        assert ledger.verify(conn, "lab") == (True, None)


def test_repeat_alert_folds_into_open_case(make_stack):
    api = make_stack().api
    post(api, SQL_ALERT)
    post(api, SQL_ALERT)
    d = wait_for(
        lambda: (x := settled_case(api, last_kind="approval_requested")) and len(x["signals"]) == 2 and x,
        what="two signals on one case",
    )
    assert kinds(d).count("diagnosis") == 1


def test_redelivered_intake_adds_nothing(make_platform):
    platform = make_platform()
    with platform.db.scope("lab") as conn:
        first = triage.ingest(conn, "lab", platform.catalog, SQL_ALERT, "intake-1")
        again = triage.ingest(conn, "lab", platform.catalog, SQL_ALERT, "intake-1")
        signals = conn.execute("SELECT count(*) AS n FROM signals").fetchone()["n"]
    assert first[0].new and not again[0].new
    assert again[0].case_id == first[0].case_id and again[0].open
    assert signals == 1


def test_intake_rejects_bad_token_and_foreign_rows(make_stack):
    api = make_stack().api
    assert api.post("/api/v1/intake/openobserve/lab", json=SQL_ALERT).status_code == 401
    assert api.post("/api/v1/intake/openobserve/lab", json=SQL_ALERT, headers=CONTOSO).status_code == 401
    assert api.post("/api/v1/intake/openobserve/nobody", json=SQL_ALERT, headers=LAB).status_code == 404
    foreign = copy.deepcopy(SQL_ALERT)
    foreign["rows"][0]["tenant_id"] = "contoso"
    assert api.post("/api/v1/intake/openobserve/lab", json=foreign, headers=LAB).status_code == 422
    assert api.post("/api/v1/intake/openobserve/lab", json={"rows": []}, headers=LAB).status_code == 422
    assert api.get("/api/v1/cases").status_code == 401


def test_rows_as_json_lines_text(make_stack):
    api = make_stack().api
    payload = copy.deepcopy(SQL_ALERT)
    payload["rows"] = '{"tenant_id": "lab", "host_name": "lab-sql01", "sqlserver_database_name": "FinanceERP"}\n'
    post(api, payload)
    assert settled_case(api)["case"]["target"] == "lab-sql01"


def test_unknown_alert_escalates_without_running_anything(make_stack):
    api = make_stack().api
    post(api, {"alert_id": "cpu-steal-high", "rows": [{"tenant_id": "lab", "host_name": "lab-app01"}]})
    d = settled_case(api)
    assert d["case"]["status"] == "escalated"
    assert d["proposals"] == []
    assert "run_started" not in kinds(d)


def test_target_outside_the_customer_estate_escalates(make_stack):
    api = make_stack().api
    payload = copy.deepcopy(SQL_ALERT)
    payload["rows"][0]["host_name"] = "cto-sql01"  # contoso's server, reported on lab's intake
    post(api, payload)
    d = settled_case(api)
    assert d["case"]["status"] == "escalated"
    assert "not a known server" in d["timeline"][-1]["body"]["reason"]


def test_runbook_checks_can_escalate_in_the_dry_run(make_stack, fakes_client):
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
    api = make_stack().api
    post(api, SQL_ALERT)
    d = settled_case(api)
    assert d["case"]["status"] == "escalated"
    assert "open transaction" in d["timeline"][-1]["body"]["reason"]


def test_rejection_escalates(make_stack):
    api = make_stack().api
    post(api, SQL_ALERT)
    pid = settled_case(api, last_kind="approval_requested")["proposals"][0]["id"]
    resp = api.post(f"/api/v1/proposals/{pid}/reject", json={"who": "Priya", "note": "month end close"}, headers=CONSOLE)
    assert resp.status_code == 200, resp.text
    d = settled_case(api, ("escalated",))
    assert d["proposals"][0]["status"] == "rejected"
    assert "run_started" not in kinds(d)[5:]


def test_unanswered_approval_escalates(make_stack):
    api = make_stack(approval_timeout_hours=1 / 3600).api  # one second
    post(api, SQL_ALERT)
    d = settled_case(api, ("escalated",))
    assert d["proposals"][0]["status"] == "rejected" and d["proposals"][0]["decided_by"] == "timeout"
    assert "no engineer decided" in d["timeline"][-1]["body"]["reason"]


def test_run_and_notify_runs_after_a_clean_dry_run(make_stack):
    api = make_stack().api
    post(
        api,
        {"alert_id": "print-spooler-stopped", "rows": [{"tenant_id": "contoso", "host_name": "cto-prt01"}]},
        tenant="contoso",
        headers=CONTOSO,
    )
    d = settled_case(api, ("resolved", "escalated"))
    assert d["case"]["status"] == "resolved"
    assert kinds(d) == [
        "case_opened",
        "diagnosis",
        "proposal",
        "run_started",
        "run_result",
        "approved",
        "run_started",
        "run_result",
    ]
    assert d["timeline"][5]["actor"] == "policy"


def test_guard_refusal_is_reported_as_failed(make_stack):
    """If an approval reference were ever lost, the runbook's guard refuses and the case escalates."""
    stack = make_stack()
    api = stack.api
    post(api, SQL_ALERT)
    pid = settled_case(api, last_kind="approval_requested")["proposals"][0]["id"]
    original = stack.platform.semaphore.start

    def start_without_ref(project_id, runbook_id, extra_vars):
        extra_vars.pop("rb_approval_ref", None)
        return original(project_id, runbook_id, extra_vars)

    stack.platform.semaphore.start = start_without_ref
    assert api.post(f"/api/v1/proposals/{pid}/approve", json={"who": "Priya"}, headers=CONSOLE).status_code == 200
    d = settled_case(api, ("escalated",))
    assert d["proposals"][0]["outcome"] == "failed"


def test_case_events_reach_nats(make_stack):
    import asyncio
    import json

    from estate_ops.bus import Bus

    stack = make_stack()
    post(stack.api, SQL_ALERT)
    settled_case(stack.api, last_kind="approval_requested")
    s = stack.platform.settings

    async def read():
        bus = await Bus.connect(s.nats_url, s.nats_prefix)
        sub = await bus.js.pull_subscribe(f"{s.nats_prefix}.events.lab.>", stream=bus.events_stream)
        out = []
        while True:
            try:
                msgs = await sub.fetch(20, timeout=1)
            except TimeoutError:
                break
            out += [json.loads(m.data)["kind"] for m in msgs]
        await bus.close()
        return out

    events = wait_for(lambda: (e := asyncio.run(read())) and "approval_requested" in e and e, what="relayed events")
    assert events[:2] == ["case_opened", "diagnosis"]
