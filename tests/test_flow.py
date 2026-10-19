"""The two first scenarios end to end, as in production: alerts -> NATS -> triage and correlation -> Temporal
workflow -> investigation -> OPA -> dry run -> approval (or policy) -> live run -> verified, on the simulator."""

from __future__ import annotations

from conftest import alert, post_alerts, wait_for

import estate_sim.app as sim
from estate_ops import ledger, sources, triage

CONSOLE = {"Authorization": "Bearer console-test-token"}
SETTLED = ("awaiting_approval", "resolved", "escalated")


def cases(api, **params):
    return api.get("/api/v1/cases", params=params, headers=CONSOLE).json()


def detail(api, case_id):
    resp = api.get(f"/api/v1/cases/{case_id}", headers=CONSOLE)
    assert resp.status_code == 200, resp.text
    return resp.json()


def settled(api, tenant: str, statuses=SETTLED, last_kind: str | None = None, timeout: float = 40):
    """The tenant's only case, once it reaches one of statuses (and, if given, its ledger ends with last_kind)."""

    def check():
        found = cases(api, tenant=tenant)
        if len(found) == 1 and found[0]["status"] in statuses:
            d = detail(api, found[0]["id"])
            if last_kind is None or (d["timeline"] and d["timeline"][-1]["kind"] == last_kind):
                return d
        return None

    return wait_for(check, timeout, what=f"a {tenant} case in {statuses}")


def kinds(d) -> list[str]:
    return [e["kind"] for e in d["timeline"]]


def checkout_storm() -> list[dict]:
    """What vmalert posts when the ETL job saturates CTO-DS-CRIT-02: about twenty alerts on four layers."""
    shop = ["SHOP-SQL01", "SHOP-API01", "SHOP-API02", "SHOP-API03", "SHOP-WEB01", "SHOP-WEB02", "SHOP-WEB03", "SHOP-WEB04"]
    shop += ["SHOP-CMS01", "SHOP-CMS02", "SHOP-SEARCH01", "BI-ETL01"]
    out = [alert("ServiceLatencyHigh", "contoso", {"service": "Checkout"}, value="2540 ms p95")]
    out += [alert("ServiceErrorsHigh", "contoso", {"service": "Checkout"}, value="3.2% errors")]
    out += [alert("ServerDiskLatencyHigh", "contoso", {"host": f"CTO-{h}"}, "P3", value="44.4 ms") for h in shop]
    out += [alert("DatastoreLatencyHigh", "contoso", {"datastore": "CTO-DS-CRIT-02"}, value="38.2 ms")]
    out += [alert("ServiceLatencyHigh", "contoso", {"service": "Online shop"}, value="3100 ms p95")]
    out += [alert("ServiceLatencyHigh", "contoso", {"service": "Sales analytics"}, "P3", value="9450 ms p95")]
    out += [alert("HostCPUHigh", "contoso", {"host": "CTO-BI-ETL01"}, "P4", value="92%")]
    return out


def test_scenario_1_checkout_storm_becomes_one_case_and_is_fixed_after_approval(make_stack, estate):
    stack = make_stack()
    api = stack.api
    estate.post("/_sim/faults/checkout-slow")
    storm = checkout_storm()
    # An unrelated alert at the same customer, and one at the other customer, at the same moment.
    other = alert("WindowsServiceStopped", "contoso", {"host": "CTO-APP001", "win_service": "W32Time"}, "P3")
    elsewhere = alert("HostDiskFull", "fabrikam", {"host": "FAB-APP010", "mount": "C:"}, "P3")
    resp = post_alerts(api, storm[:1])
    assert resp.status_code == 200, resp.text
    assert post_alerts(api, [*storm[1:], other, elsewhere]).status_code == 200

    def storm_case():
        for c in cases(api, tenant="contoso"):
            if c["signals"] == len(storm) and c["status"] == "awaiting_approval":
                return detail(api, c["id"])
        return None

    d = wait_for(storm_case, 40, "the checkout case to wait for approval")
    assert len(cases(api, tenant="contoso")) == 2  # the storm, and the unrelated service alert
    assert len(cases(api, tenant="fabrikam")) == 1
    case = d["case"]
    assert case["severity"] == "P2" and case["respond_due"] and case["responded_at"]
    assert "datastore:CTO-DS-CRIT-02" in case["assets"] and "service:Checkout" in case["assets"]
    assert d["blast_radius"]["critical"] is True

    investigation = next(e["body"] for e in d["timeline"] if e["kind"] == "investigation")
    assert investigation["method"] == "deterministic"
    assert investigation["root_asset"] == "datastore:CTO-DS-CRIT-02"
    assert investigation["evidence"][0]["source"] == "datastore:CTO-DS-CRIT-02"

    [p] = d["proposals"]
    assert (p["skill_id"], p["targets"], p["mode"]) == ("vmware/rebalance-datastore", "CTO-DS-CRIT-02", "approve_to_run")
    assert p["policy"]["reason"] == "autonomy for vmware/rebalance-datastore at contoso is approve_to_run"
    plan = p["dry_run_evidence"]["decision"]["plan"]
    assert plan == [plan[0] | {"vm": "CTO-BI-ETL01", "from": "CTO-DS-CRIT-02", "to": "CTO-DS-GEN-02"}]
    assert sim.ESTATES["contoso"].vms["CTO-BI-ETL01"].datastore == "CTO-DS-CRIT-02"  # nothing moved yet

    resp = api.post(f"/api/v1/proposals/{p['id']}/approve", json={"who": "Priya", "note": "go"}, headers=CONSOLE)
    assert resp.status_code == 200, resp.text
    again = api.post(f"/api/v1/proposals/{p['id']}/approve", json={"who": "Sam"}, headers=CONSOLE)
    assert again.status_code == 409

    d = wait_for(lambda: (x := detail(api, case["id"]))["case"]["status"] in ("resolved", "escalated") and x, 40, "resolution")
    assert d["case"]["status"] == "resolved", d["timeline"][-1]
    assert sim.ESTATES["contoso"].vms["CTO-BI-ETL01"].datastore == "CTO-DS-GEN-02"
    assert sim.ESTATES["contoso"].service_latency("Checkout") < 400
    k = kinds(d)
    assert k[0] == "case_opened" and k.count("alert_folded") == len(storm) - 1
    assert k[len(storm) :] == [
        "investigation",
        "proposal",
        "run_result",
        "approval_requested",
        "approved",
        "run_result",
        "verified",
        "resolved",
    ]
    live = d["timeline"][-3]["body"]
    assert live["dry_run"] is False and live["evidence"]["outcome"] == "fixed"
    with stack.platform.db.scope("contoso") as conn:
        assert ledger.verify(conn, "contoso") == (True, None)


def test_scenario_2_backup_fixed_by_policy_without_a_person(make_stack, estate):
    stack = make_stack()
    api = stack.api
    estate.post("/_sim/faults/backup-repo-full")
    platform = stack.platform
    tenant = platform.tenants.get("fabrikam")
    polled = sources.poll(platform, tenant, sources.POLLERS[0])
    failed = [(i, p) for i, p in polled if p["event"] == "firing"]
    assert [p["rows"][0]["backup_job"] for _, p in failed] == ["EHR SQL nightly"]
    with platform.db.scope("fabrikam") as conn:
        triage.ingest(conn, tenant, failed[0][1], failed[0][0], platform.graph)
        # The same session polled again adds nothing.
        triage.ingest(conn, tenant, failed[0][1], failed[0][0], platform.graph)
    repo_low = alert("BackupRepositoryLow", "fabrikam", {"repository": "FAB-REPO-01"}, value="0.4% free")
    assert post_alerts(api, [repo_low]).status_code == 200
    d = settled(api, "fabrikam", ("resolved", "escalated"), last_kind="resolved")
    assert d["case"]["status"] == "resolved", d["timeline"][-1]
    assert d["case"]["service"] == "EHR"
    assert len(d["signals"]) == 2
    [p] = d["proposals"]
    assert (p["skill_id"], p["targets"], p["mode"], p["decided_by"]) == (
        "backup/free-space-and-rerun",
        "EHR SQL nightly",
        "run_and_notify",
        "policy",
    )
    assert [a["did"] for a in p["evidence"]["actions"]] == ["remove_backup", "remove_backup", "start_job"]
    assert p["evidence"]["after"]["result"] == "success"
    assert sim.ESTATES["fabrikam"].repository_free("FAB-REPO-01") >= 0.05
    assert "approval_requested" not in kinds(d) and "approved" in kinds(d)
    # The next poll clears the alert, which lands on no open case.
    polled = sources.poll(platform, tenant, sources.POLLERS[0])
    cleared = [p for _, p in polled if p["rows"][0]["backup_job"] == "EHR SQL nightly"]
    assert cleared[0]["event"] == "cleared"


def test_rejection_escalates_and_changes_nothing(make_stack, estate):
    stack = make_stack()
    api = stack.api
    estate.post("/_sim/faults/checkout-slow")
    post_alerts(api, [alert("DatastoreLatencyHigh", "contoso", {"datastore": "CTO-DS-CRIT-02"})])
    d = settled(api, "contoso", last_kind="approval_requested")
    p = d["proposals"][0]
    resp = api.post(f"/api/v1/proposals/{p['id']}/reject", json={"who": "Sam", "note": "not during trading"}, headers=CONSOLE)
    assert resp.status_code == 200
    d = settled(api, "contoso", ("escalated",))
    assert "Sam rejected the skill: not during trading" in d["timeline"][-1]["body"]["reason"]
    assert sim.ESTATES["contoso"].vms["CTO-BI-ETL01"].datastore == "CTO-DS-CRIT-02"


def test_policy_service_down_means_recommend_only(make_stack, estate):
    from estate_ops.policy import Policy

    stack = make_stack(policy=Policy("http://127.0.0.1:9"))
    api = stack.api
    estate.post("/_sim/faults/spooler-stopped")
    post_alerts(api, [alert("WindowsServiceStopped", "fabrikam", {"host": "FAB-APP003", "win_service": "Spooler"}, "P3")])
    d = settled(api, "fabrikam", ("escalated",))
    [p] = d["proposals"]
    assert p["mode"] == "recommend" and "could not be reached" in p["policy"]["reason"]
    assert d["timeline"][-1]["body"]["engineer_should_check"].startswith("Run the dry run's plan by hand")
    assert sim.ESTATES["fabrikam"].vms["FAB-APP003"].services["Spooler"] == "stopped"


def test_low_risk_skill_runs_and_notifies(make_stack, estate):
    stack = make_stack()
    estate.post("/_sim/faults/spooler-stopped")
    post_alerts(stack.api, [alert("WindowsServiceStopped", "fabrikam", {"host": "FAB-APP003", "win_service": "Spooler"}, "P3")])
    d = settled(stack.api, "fabrikam", ("resolved", "escalated"), last_kind="resolved")
    assert d["proposals"][0]["params"]["service"] == "Spooler"
    assert sim.ESTATES["fabrikam"].vms["FAB-APP003"].services["Spooler"] == "running"


def test_an_alert_no_skill_answers_is_escalated_with_the_evidence(make_stack, estate):
    stack = make_stack()
    post_alerts(stack.api, [alert("CertificateExpiring", "contoso", {"host": "CTO-SHOP-WEB01", "certificate": "x"}, "P3")])
    d = settled(stack.api, "contoso", ("escalated",))
    assert d["proposals"] == []
    assert d["timeline"][-1]["body"]["reason"] == "no skill answers this case"
