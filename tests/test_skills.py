"""The skills library: every skill's decide() cases from its tests.yml, and the scenario skills run live
against the simulated estate. Needs no database, graph or telemetry service."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
import yaml
from conftest import SimMetrics
from fastapi.testclient import TestClient

import estate_sim.app as sim
from estate_ops import skills as lib
from estate_ops.connectors import Connectors
from estate_ops.tenancy import Tenant

ROOT = Path(__file__).resolve().parents[1] / "skills"
LIBRARY = lib.Skills.load(ROOT)


def _cases():
    for skill in LIBRARY:
        spec = yaml.safe_load((skill.path / "tests.yml").read_text(encoding="utf-8"))
        for case in spec["cases"]:
            yield pytest.param(skill.id, spec.get("defaults", {}), case, id=f"{skill.id}: {case['name']}")


def test_every_skill_has_tests_and_valid_metadata():
    assert LIBRARY.ids() == [
        "backup/free-space-and-rerun",
        "vmware/rebalance-datastore",
        "windows/cleanup-disk",
        "windows/restart-service",
    ]
    for skill in LIBRARY:
        assert (skill.path / "tests.yml").is_file(), skill.id
        LIBRARY.module(skill.id)
        _, errors = LIBRARY.validate_params(skill.id, {"service": "Spooler"} if skill.id == "windows/restart-service" else {})
        assert errors == []


@pytest.mark.parametrize(("skill_id", "defaults", "case"), list(_cases()))
def test_decide(skill_id, defaults, case):
    params, errors = LIBRARY.validate_params(skill_id, case.get("params", defaults))
    assert errors == []
    decision = LIBRARY.module(skill_id).decide(case["facts"], params)
    assert decision["action"] == case["expect"], decision["reason"]
    for want, got in zip(case.get("plan", []), decision["plan"], strict=False):
        assert {k: got.get(k) for k in want} == want


def test_params_from_alert_labels():
    assert LIBRARY.params_from_alert(
        "windows/restart-service", "WindowsServiceStopped", {"win_service": "Spooler", "host": "X"}
    ) == {"service": "Spooler"}
    assert LIBRARY.for_alert("DatastoreLatencyHigh") == ["vmware/rebalance-datastore"]


def test_bad_params_are_refused():
    _, errors = LIBRARY.validate_params("vmware/rebalance-datastore", {"latency_threshold_ms": 1000, "bogus": 1})
    assert len(errors) == 2


# ---- live runs against the simulator ----------------------------------------------------------------------


class Settings:
    metrics_url = logs_url = "http://unused"


class Graph:
    """Blast radius as the declared services say: critical services' servers are protected."""

    def blast_radius(self, tenant_id, ids):
        e = sim.ESTATES[tenant_id]
        names = {i.split(":", 1)[1] for i in ids}
        names |= {v.name for v in e.vms.values() if v.datastore in names}  # a datastore's radius is its VMs' services
        services = [s for s in e.services.values() if names & set(s["vms"])]
        return {"services": [s["name"] for s in services], "critical": any(s["critical"] for s in services)}


def _connectors(client: TestClient, tenant_id: str) -> Connectors:
    base = f"http://sim:8095/{tenant_id}"
    tenant = Tenant(
        id=tenant_id,
        name=tenant_id.title(),
        intake_token_env="X",
        connectors={
            "vcenter": {"url": f"{base}/vcenter", "username_env": "U", "password_env": "P"},
            "veeam": {"url": f"{base}/veeam", "username_env": "U", "password_env": "P"},
            "edge": {"url": f"{base}/edge", "token_env": "P"},
        },
    )
    conns = Connectors(tenant, Settings(), env={"U": "svc", "P": "secret"}, http=client)
    conns.metrics = lambda: SimMetrics(client, tenant_id)
    return conns


def _run(client, tenant_id, skill_id, target, params, live, approval_ref=None):
    conns = _connectors(client, tenant_id)
    clean, errors = LIBRARY.validate_params(skill_id, params)
    assert errors == []
    ctx = lib.Context(
        tenant=conns.tenant,
        target=target,
        params=clean,
        skill=LIBRARY.get(skill_id),
        _connectors=conns,
        graph=Graph(),
        dry_run=not live,
        approval_ref=approval_ref,
        sleep=lambda s: None,
    )
    return lib.run(LIBRARY, ctx, live)


def test_scenario_1_rebalance_fixes_checkout(estate):
    estate.post("/_sim/faults/checkout-slow")
    dry = _run(estate, "contoso", "vmware/rebalance-datastore", "CTO-DS-CRIT-02", {}, live=False)
    assert dry["outcome"] == "dry_run", dry
    assert dry["decision"]["plan"][0]["vm"] == "CTO-BI-ETL01"
    refused = _run(estate, "contoso", "vmware/rebalance-datastore", "CTO-DS-CRIT-02", {}, live=True)
    assert refused["outcome"] == "failed" and "approval" in refused["error"]
    live = _run(estate, "contoso", "vmware/rebalance-datastore", "CTO-DS-CRIT-02", {}, live=True, approval_ref="CONTOSO-1/p")
    assert live["outcome"] == "fixed", live
    assert live["after"]["latency_ms"] < 20
    assert sim.ESTATES["contoso"].service_latency("Checkout") < 400


def test_scenario_2_backup_frees_space_and_reruns(estate):
    estate.post("/_sim/faults/backup-repo-full")
    live = _run(estate, "fabrikam", "backup/free-space-and-rerun", "EHR SQL nightly", {}, live=True)
    assert live["outcome"] == "fixed", live
    assert [a["did"] for a in live["actions"]] == ["remove_backup", "remove_backup", "start_job"]
    assert live["after"]["result"] == "success"
    assert sim.ESTATES["fabrikam"].repository_free("FAB-REPO-01") >= 0.05


def test_restart_service_on_the_sim(estate):
    estate.post("/_sim/faults/spooler-stopped")
    live = _run(estate, "fabrikam", "windows/restart-service", "FAB-APP003", {"service": "Spooler"}, live=True)
    assert live["outcome"] == "fixed", live


def test_a_skill_only_gets_the_connectors_it_declares(estate):
    conns = _connectors(estate, "fabrikam")
    ctx = lib.Context(conns.tenant, "FAB-APP003", {}, LIBRARY.get("windows/restart-service"), conns)
    with pytest.raises(lib.SkillError):
        ctx.connector("vcenter")


def test_vcenter_session_is_basic_auth(estate):
    token = base64.b64encode(b"svc:secret").decode()
    assert estate.post("/contoso/vcenter/api/session", headers={"Authorization": f"Basic {token}"}).status_code == 201
