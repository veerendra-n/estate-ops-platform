"""HaloPSA tickets, Teams cards, NetBox discovery, provisioning and single sign-on, against the fakes in dev/."""

from __future__ import annotations

import base64
import json

import pytest
from conftest import ROOT, SQL_ALERT, wait_for
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from test_flow import CONSOLE, post, settled_case

from dev import fakes
from estate_ops import auth
from estate_ops.api import create_app
from estate_ops.catalog import Catalog
from estate_ops.integrations import netbox, provision
from estate_ops.integrations.halopsa import HaloClient, HaloPSA
from estate_ops.integrations.teams import Teams
from estate_ops.semaphore import Semaphore

FAKES = TestClient(fakes.app, base_url="http://fakes")


def halo(platform):
    return HaloPSA(platform, HaloClient("http://fakes/halo", "id", "secret", client=FAKES))


def teams(platform):
    return Teams(platform, client=FAKES)


def inbox():
    return FAKES.get("/_inbox").json()


def test_case_becomes_a_halo_ticket_with_notes_and_closes(make_stack):
    stack = make_stack(integrations=[halo])
    api = stack.api
    post(api, SQL_ALERT)
    pid = settled_case(api, last_kind="approval_requested")["proposals"][0]["id"]
    resp = api.post(f"/api/v1/proposals/{pid}/approve", json={"who": "Priya"}, headers=CONSOLE)
    assert resp.status_code == 200, resp.text
    case = settled_case(api, ("resolved",))

    def closed():
        tickets = inbox()["halo"]["tickets"]
        return tickets and tickets[0]["status_id"] == 9 and tickets[0]

    ticket = wait_for(closed, what="the ticket to close")
    assert len(inbox()["halo"]["tickets"]) == 1
    assert ticket["summary"].startswith(f"[{case['case']['ref']}]")
    assert ticket["client_id"] == 12 and ticket["site_id"] == 34 and ticket["priority_id"] == 2  # P2
    notes = [a["note"] for a in inbox()["halo"]["actions"]]
    assert all(a["hiddenfromuser"] for a in inbox()["halo"]["actions"])
    assert notes[0].startswith("Opened by the platform from alert sql-log-used-high")
    assert any(n.startswith("Diagnosis") for n in notes)
    assert any("Waiting for an engineer to approve" in n and f"#case={case['case']['id']}" in n for n in notes)
    assert any(n.startswith("Approved by Priya") for n in notes)
    assert notes[-1].startswith("Live run: Fixed and verified")
    with stack.platform.db.scope("lab") as conn:
        ref = conn.execute("SELECT ref FROM external_refs WHERE system = 'halopsa'").fetchone()["ref"]
    assert int(ref) == ticket["id"]


def test_halo_outage_is_retried_in_order(make_stack):
    FAKES.post("/_reset")
    fakes._halo["fail_next"] = 2  # the first two calls fail
    stack = make_stack(integrations=[halo])
    post(stack.api, SQL_ALERT)
    settled_case(stack.api, last_kind="approval_requested")
    wait_for(lambda: any("Waiting for an engineer" in a["note"] for a in inbox()["halo"]["actions"]), timeout=40, what="notes")
    notes = [a["note"] for a in inbox()["halo"]["actions"]]
    assert notes[0].startswith("Opened by the platform")  # order kept through the retries
    assert len(inbox()["halo"]["tickets"]) == 1


def test_teams_cards_for_approval_and_fix(make_stack):
    stack = make_stack(integrations=[teams])
    api = stack.api
    post(api, SQL_ALERT)
    pid = settled_case(api, last_kind="approval_requested")["proposals"][0]["id"]
    wait_for(lambda: inbox()["teams"], what="the approval card")
    api.post(f"/api/v1/proposals/{pid}/approve", json={"who": "Priya"}, headers=CONSOLE)
    settled_case(api, ("resolved",))
    cards = wait_for(lambda: len(inbox()["teams"]) == 2 and inbox()["teams"], what="the fixed card")
    first = cards[0]["attachments"][0]["content"]
    assert first["body"][0]["text"].startswith("Approval needed")
    assert first["actions"][0]["url"].startswith("http://localhost:8080/#case=")
    assert cards[1]["attachments"][0]["content"]["body"][0]["text"].startswith("Fixed by runbook")
    assert {c["channel"] for c in cards} == {"lab"}


def test_tenant_without_integrations_gets_nothing(make_stack):
    stack = make_stack(integrations=[halo, teams])
    post(
        stack.api,
        {"alert_id": "print-spooler-stopped", "rows": [{"tenant_id": "contoso", "host_name": "cto-prt01"}]},
        tenant="contoso",
        headers={"Authorization": "Bearer contoso-test-token"},
    )
    settled_case(stack.api, ("resolved",))
    with stack.platform.db.scope("contoso") as conn:
        last = conn.execute("SELECT max(id) AS n FROM ledger").fetchone()["n"]
    # Give the consumers time to pass the last entry, then check they wrote nothing.
    wait_for(lambda: _consumed(stack, last), what="consumers to catch up")
    assert inbox()["halo"]["tickets"] == [] and inbox()["teams"] == []


def _consumed(stack, last_id: int) -> bool:
    import asyncio

    from estate_ops.bus import Bus

    s = stack.platform.settings

    async def pending():
        bus = await Bus.connect(s.nats_url, s.nats_prefix)
        infos = [await bus.js.consumer_info(bus.events_stream, f"int-{n}") for n in ("halopsa", "teams")]
        await bus.close()
        return all(i.num_pending == 0 and i.num_ack_pending == 0 for i in infos)

    return asyncio.run(pending())


def test_netbox_discovery_fills_the_graph(graph):
    lab = next(
        t
        for t in __import__("estate_ops.tenancy", fromlist=["Tenants"]).Tenants.load(ROOT / "config/tenants.yml")
        if t.id == "lab"
    )
    client = netbox.NetBoxClient("http://fakes/netbox", "abc123", client=FAKES)
    result = netbox.discover(client, graph, lab)
    assert result["assets"] == 6  # cluster, 3 named devices, 2 VMs; the unnamed patch panel is skipped
    sql = graph.host("lab", "lab-sql01")
    assert sql["attrs"]["ip"] == "10.20.0.11" and sql["attrs"]["virtual"] is True
    hood = graph.neighbourhood("lab", "host:lab-sql01", hops=2)
    kinds = {a["id"]: a["kind"] for a in hood["assets"]}
    assert kinds["hv:esx-lab-01"] == "hypervisor_host" and kinds["cluster:lab-cluster"] == "hypervisor_cluster"
    assert {"src": "host:lab-sql01", "rel": "runs_on", "dst": "hv:esx-lab-01"} in hood["relations"]
    # The seed's database still hangs off the server NetBox now describes.
    assert "sqlinstance:lab-sql01/MSSQLSERVER" in kinds
    assert graph.host("contoso", "lab-sql01") is None
    # A VM removed from NetBox leaves the graph on the next run.
    fakes._netbox_removed.add("lab-web01")
    assert netbox.discover(client, graph, lab)["removed"] == 1
    assert graph.host("lab", "lab-web01") is None
    fakes._netbox_removed.clear()


def test_netbox_v2_tokens_use_bearer():
    assert netbox.NetBoxClient("http://x", "nbt_abc.def").headers["Authorization"] == "Bearer nbt_abc.def"
    assert netbox.NetBoxClient("http://x", "0123abcd").headers["Authorization"] == "Token 0123abcd"


def test_provision_semaphore_templates(make_platform, monkeypatch):
    FAKES.post("/_reset")
    platform = make_platform()
    fakes._projects[1] = [{"id": 1, "name": "sql/log-backup-shrink"}]  # one already there
    lab = platform.tenants.get("lab")
    sem = Semaphore("http://fakes/semaphore", "t", client=TestClient(fakes.app, base_url="http://fakes/semaphore"))
    report = provision.semaphore_templates(sem, platform.catalog, lab)
    assert report["existing"] == ["sql/log-backup-shrink"]
    assert len(report["created"]) == len(platform.catalog.runbook_ids()) - 1
    made = {t["name"]: t for t in fakes._projects[1]}
    assert made["windows/restart-spooler"]["playbook"] == "ansible/runbooks/windows/restart-spooler/runbook.yml"
    assert made["windows/restart-spooler"]["inventory_id"] == 1
    assert provision.semaphore_templates(sem, platform.catalog, lab)["created"] == []  # idempotent
    with pytest.raises(ValueError, match="semaphore needs"):
        provision.semaphore_templates(sem, platform.catalog, platform.tenants.get("contoso"))


def test_provision_openobserve_alerts(make_platform):
    FAKES.post("/_reset")
    platform = make_platform()
    admin = provision.OpenObserveAdmin(
        "http://fakes/openobserve", None, None, client=TestClient(fakes.app, base_url="http://fakes/openobserve")
    )
    report = admin.provision(platform.catalog, platform.tenants.get("lab"), "https://ops.example.net")
    assert "destination estate-ops-lab" in report["created"]
    org = inbox()["openobserve"]["lab"]
    [dest] = org["destinations"]
    assert dest["url"] == "https://ops.example.net/api/v1/intake/openobserve/lab"
    assert dest["headers"]["Authorization"] == "Bearer lab-test-token"
    alerts = {a["name"]: a for a in org["alerts"]}
    assert set(alerts) == {a.id for a in platform.catalog.alerts() if a.query}
    sql = alerts["sql-log-used-high"]
    assert sql["trigger_condition"]["period"] > 0 and sql["destinations"] == ["estate-ops-lab"]
    row = json.loads(sql["row_template"])
    assert row["tenant_id"] == "{tenant_id}" and "sqlserver_database_name" in row
    # A rendered webhook body is what the intake parses.
    body = org["templates"][0]["body"].replace("{rows}", json.dumps([{"tenant_id": "lab", "host_name": "lab-sql01"}]))
    assert json.loads(body.replace("{alert_name}", "x").replace("{org_name}", "lab"))["rows"][0]["host_name"] == "lab-sql01"
    again = admin.provision(platform.catalog, platform.tenants.get("lab"), "https://ops.example.net")
    assert again["created"] == []


# ---- single sign-on ---------------------------------------------------------------------------------------


def _session_cookie(secret: str, data: dict) -> str:
    """The cookie Starlette's SessionMiddleware would set for this session."""
    payload = base64.b64encode(json.dumps(data).encode())
    return TimestampSigner(secret).sign(payload).decode()


def test_sso_signs_approvals_with_the_token_identity(make_stack):
    stack = make_stack(
        oidc_issuer="https://login.microsoftonline.com/tenant-1/v2.0", oidc_client_id="app", session_secret="s" * 32
    )
    api = stack.api
    post(api, SQL_ALERT)
    # The shared token no longer opens the console.
    assert api.get("/api/v1/cases", headers=CONSOLE).status_code == 401
    assert api.get("/auth/me").json() == {"sso": True, "user": None, "signed_in": False}
    user = {"sub": "abc", "iss": "x", "name": "Priya Nair", "email": "priya@msp.example"}
    api.cookies.set("eo_session", _session_cookie("s" * 32, {"user": user}))
    pid = settled_case(api, last_kind="approval_requested")["proposals"][0]["id"]
    resp = api.post(f"/api/v1/proposals/{pid}/approve", json={"who": "Somebody Else"})
    assert resp.status_code == 200, resp.text
    d = settled_case(api, ("resolved",))
    approved = next(e for e in d["timeline"] if e["kind"] == "approved")
    assert approved["actor"] == "engineer:Priya Nair <priya@msp.example>"


def test_sso_needs_a_session_secret(make_platform):
    platform = make_platform(oidc_issuer="https://accounts.google.com", oidc_client_id="app")
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        create_app(platform)


def test_allowed_claims():
    assert auth.allowed({"tid": "T1"}, ())
    assert auth.allowed({"tid": "t1"}, ("t1",))
    assert not auth.allowed({"tid": "t2"}, ("t1",))
    assert auth.allowed({"hd": "msp.example", "email": "a@msp.example", "email_verified": True}, ("msp.example",))
    assert not auth.allowed({"hd": "msp.example", "email": "a@msp.example", "email_verified": False}, ("msp.example",))


def test_catalog_alerts_keep_query_fields():
    alert = Catalog.load(ROOT / "tests/fixtures/catalog.json").alert("sql-log-used-high")
    assert alert.query and alert.stream_type in ("logs", "metrics") and alert.period
