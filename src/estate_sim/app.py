"""The simulator's HTTP side: vendor-shaped APIs per customer, Prometheus metrics, OTLP logs, fault control.

    /{tenant}/vcenter/api/...                 vSphere Automation REST (session, cluster, host, datastore, vm, relocate)
    /{tenant}/veeam/api/...                   Veeam B&R REST (token, jobs, sessions, repositories, backups, start)
    /{tenant}/login/{entra}/oauth2/v2.0/token Entra ID client credentials
    /{tenant}/graph/{v1.0|beta}/...           Microsoft Graph (managedDevices, users, service health, actions)
    /{tenant}/fleet/api/v1/fleet/...          Fleet (hosts, live query)
    /{tenant}/edge/v1/ops/{op}                the edge node's typed operations
    /metrics                                  every customer's telemetry, scraped by the OpenTelemetry Collector
    /_sim/...                                 faults on and off, reset, recent events (the cockpit's Lab page)

Credentials are checked for presence only. Run: uvicorn estate_sim.app:app --port 8095
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from . import model

log = logging.getLogger("estate_sim")
OTLP_LOGS_URL = os.environ.get("OTLP_LOGS_URL")  # e.g. http://otel-collector:4318/v1/logs
JOB_SECONDS = float(os.environ.get("SIM_JOB_SECONDS", "90"))

ESTATES: dict[str, model.Estate] = model.build_all()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_ship_logs()) if OTLP_LOGS_URL else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Estate simulator", lifespan=lifespan)


def estate(tenant: str) -> model.Estate:
    try:
        return ESTATES[tenant]
    except KeyError:
        raise HTTPException(404, "no such customer in the simulator") from None


def _bearer(request: Request) -> None:
    if not request.headers.get("authorization", "").lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")


# ---- vCenter ---------------------------------------------------------------------------------------------


@app.post("/{tenant}/vcenter/api/session", status_code=201)
def vc_session(tenant: str, request: Request) -> str:
    estate(tenant)
    if not request.headers.get("authorization", "").lower().startswith("basic "):
        raise HTTPException(401, "basic auth required")
    return f"vc-session-{tenant}"


def _vc(request: Request, tenant: str) -> model.Estate:
    if request.headers.get("vmware-api-session-id") != f"vc-session-{tenant}":
        raise HTTPException(401, "not authenticated")
    return estate(tenant)


@app.delete("/{tenant}/vcenter/api/session", status_code=204)
def vc_logout(tenant: str) -> None:
    return None


@app.get("/{tenant}/vcenter/api/vcenter/cluster")
def vc_clusters(tenant: str, request: Request) -> list[dict[str, Any]]:
    e = _vc(request, tenant)
    return [{"cluster": f"domain-c{i}", "name": c, "ha_enabled": True, "drs_enabled": True} for i, c in enumerate(e.clusters, 1)]


@app.get("/{tenant}/vcenter/api/vcenter/host")
def vc_hosts(tenant: str, request: Request, clusters: str | None = None) -> list[dict[str, Any]]:
    e = _vc(request, tenant)
    names = list(e.clusters)
    wanted = None if clusters is None else names[int(clusters.removeprefix("domain-c")) - 1]
    return [
        {"host": f"host-{i}", "name": h["name"], "connection_state": "CONNECTED", "power_state": "POWERED_ON"}
        for i, h in enumerate(e.hosts.values(), 1)
        if wanted is None or h["cluster"] == wanted
    ]


@app.get("/{tenant}/vcenter/api/vcenter/datastore")
def vc_datastores(tenant: str, request: Request) -> list[dict[str, Any]]:
    e = _vc(request, tenant)
    return [
        {
            "datastore": d.id,
            "name": d.name,
            "type": "VMFS",
            "capacity": d.capacity_gb * model.GB,
            "free_space": (d.capacity_gb - d.used_gb) * model.GB,
        }
        for d in e.datastores.values()
    ]


def _host_id(e: model.Estate, name: str) -> str:
    return f"host-{list(e.hosts).index(name) + 1}"


def _vm_summary(e: model.Estate, vm: model.VM) -> dict[str, Any]:
    return {"vm": vm.id, "name": vm.name, "power_state": vm.power, "cpu_count": vm.vcpus, "memory_size_MiB": vm.memory_mb}


@app.get("/{tenant}/vcenter/api/vcenter/vm")
def vc_vms(tenant: str, request: Request, hosts: str | None = None, names: str | None = None, datastores: str | None = None):
    e = _vc(request, tenant)
    out = []
    for vm in e.vms.values():
        if hosts and _host_id(e, vm.host) != hosts:
            continue
        if names and vm.name != names:
            continue
        if datastores and e.datastores[vm.datastore].id != datastores:
            continue
        out.append(_vm_summary(e, vm))
    return out


def _vm_by_id(e: model.Estate, vm_id: str) -> model.VM:
    vm = next((v for v in e.vms.values() if v.id == vm_id), None)
    if vm is None:
        raise HTTPException(404, "no such vm")
    return vm


@app.get("/{tenant}/vcenter/api/vcenter/vm/{vm_id}")
def vc_vm(tenant: str, vm_id: str, request: Request) -> dict[str, Any]:
    e = _vc(request, tenant)
    vm = _vm_by_id(e, vm_id)
    ds = e.datastores[vm.datastore]
    return {
        "name": vm.name,
        "power_state": vm.power,
        "guest_OS": "WINDOWS_SERVER_2021" if "Windows" in vm.os else "UBUNTU_64",
        "cpu": {"count": vm.vcpus},
        "memory": {"size_MiB": vm.memory_mb},
        "disks": {
            "2000": {
                "label": "Hard disk 1",
                "capacity": vm.disk_gb * model.GB,
                "backing": {"vmdk_file": f"[{ds.name}] {vm.name}/{vm.name}.vmdk"},
            }
        },
        # The simulator adds placement so skills can see it in one call; real vCenter needs a lookup per host.
        "placement": {"host": _host_id(e, vm.host), "host_name": vm.host, "datastore": ds.id, "datastore_name": ds.name},
        "io_load": round(vm.io_load),
    }


@app.post("/{tenant}/vcenter/api/vcenter/vm/{vm_id}")
async def vc_vm_action(tenant: str, vm_id: str, action: str, request: Request) -> None:
    e = _vc(request, tenant)
    if action != "relocate":
        raise HTTPException(400, "unsupported action")
    vm = _vm_by_id(e, vm_id)
    placement = (await request.json()).get("placement") or {}
    if placement.get("datastore"):
        ds = next((d for d in e.datastores.values() if d.id == placement["datastore"]), None)
        if ds is None:
            raise HTTPException(400, "no such datastore")
        e.event("storage vMotion", vm=vm.name, source=vm.datastore, destination=ds.name)
        vm.datastore = ds.name
    if placement.get("host"):
        name = next(h for h in e.hosts if _host_id(e, h) == placement["host"])
        e.event("vMotion", vm=vm.name, source=vm.host, destination=name)
        vm.host = name


@app.patch("/{tenant}/vcenter/api/vcenter/vm/{vm_id}/hardware/disk/{disk_id}")
async def vc_grow_disk(tenant: str, vm_id: str, disk_id: str, request: Request) -> None:
    e = _vc(request, tenant)
    vm = _vm_by_id(e, vm_id)
    capacity = int((await request.json())["capacity"]) // model.GB
    if capacity < vm.disk_gb:
        raise HTTPException(400, "disks can only grow")
    vm.disk_used = round(vm.disk_used * vm.disk_gb / capacity, 3)
    e.event("disk grown", vm=vm.name, from_gb=vm.disk_gb, to_gb=capacity)
    vm.disk_gb = capacity


# ---- Veeam -----------------------------------------------------------------------------------------------


@app.post("/{tenant}/veeam/api/oauth2/token")
async def veeam_token(tenant: str, request: Request) -> dict[str, Any]:
    estate(tenant)
    if not request.headers.get("x-api-version"):
        raise HTTPException(400, "x-api-version header is required")
    form = dict(x.split("=", 1) for x in (await request.body()).decode().split("&") if "=" in x)
    if form.get("grant_type") != "password" or not form.get("username"):
        raise HTTPException(401, "bad grant")
    return {"access_token": f"veeam-{tenant}", "token_type": "bearer", "expires_in": 900}


def _veeam(request: Request, tenant: str) -> model.Estate:
    if request.headers.get("authorization") != f"Bearer veeam-{tenant}" or not request.headers.get("x-api-version"):
        raise HTTPException(401, "not authenticated")
    return estate(tenant)


def _page(items: list[dict[str, Any]], skip: int, limit: int) -> dict[str, Any]:
    return {
        "data": items[skip : skip + limit],
        "pagination": {"total": len(items), "count": len(items[skip : skip + limit]), "skip": skip, "limit": limit},
    }


def _finish_running(e: model.Estate) -> None:
    for job in e.jobs.values():
        s = job.sessions[-1]
        if s["state"] == "Working" and time.time() - s["_started"] >= JOB_SECONDS:
            # An incremental run writes about 40 GB; it fails if that would leave the repository under 5% free.
            free = e.repository_free_gb(job.repository)
            ok = free - 40 >= 0.05 * e.repositories[job.repository]["capacity_gb"]
            s.update(state="Stopped", endTime=model._iso(time.time()))
            s["result"] = {
                "result": "Success" if ok else "Failed",
                "message": "Job finished successfully" if ok else f"Insufficient free space on repository {job.repository}",
                "isCanceled": False,
            }
            if ok:
                e.backups[f"inc-{len(e.backups)}"] = model.Backup(f"inc-{len(e.backups)}", job.name, job.id, job.repository, 40)
            e.event("backup job finished", job=job.name, result=s["result"]["result"])


@app.get("/{tenant}/veeam/api/v1/jobs")
def veeam_jobs(tenant: str, request: Request, skip: int = 0, limit: int = 200, nameFilter: str | None = None) -> dict[str, Any]:  # noqa: N803
    e = _veeam(request, tenant)
    items = [
        {
            "id": j.id,
            "name": j.name,
            "type": "VSphereBackup",
            "isDisabled": False,
            "description": "",
            "virtualMachines": {
                "includes": [{"type": "VirtualMachine", "hostName": "vcenter", "name": v, "platform": "VMware"} for v in j.vms]
            },
            "storage": {"backupRepositoryId": e.repositories[j.repository]["id"]},
        }
        for j in e.jobs.values()
        if not nameFilter or nameFilter.replace("*", "") in j.name
    ]
    return _page(items, skip, limit)


@app.post("/{tenant}/veeam/api/v1/jobs/{job_id}/start")
def veeam_start(tenant: str, job_id: str, request: Request) -> dict[str, Any]:
    e = _veeam(request, tenant)
    job = next((j for j in e.jobs.values() if j.id == job_id), None)
    if job is None:
        raise HTTPException(404, "no such job")
    _finish_running(e)
    if job.sessions[-1]["state"] == "Working":
        raise HTTPException(409, "the job is already running")
    session = model._session(job, "None", time.time(), "", state="Working")
    session["_started"] = time.time()
    session["id"] = f"{job.id[:8]}-run-{len(job.sessions)}"
    job.sessions.append(session)
    e.event("backup job started", job=job.name)
    return {k: v for k, v in session.items() if not k.startswith("_")}


@app.get("/{tenant}/veeam/api/v1/sessions")
def veeam_sessions(
    tenant: str, request: Request, skip: int = 0, limit: int = 200, endedAfterFilter: str | None = None
) -> dict[str, Any]:  # noqa: N803
    e = _veeam(request, tenant)
    _finish_running(e)
    items = [{k: v for k, v in s.items() if not k.startswith("_")} for j in e.jobs.values() for s in j.sessions]
    if endedAfterFilter:
        items = [s for s in items if s["endTime"] is None or s["endTime"] >= endedAfterFilter[:19] + "Z"]
    items.sort(key=lambda s: s["creationTime"])
    return _page(items, skip, limit)


@app.get("/{tenant}/veeam/api/v1/sessions/{session_id}")
def veeam_session(tenant: str, session_id: str, request: Request) -> dict[str, Any]:
    e = _veeam(request, tenant)
    _finish_running(e)
    for j in e.jobs.values():
        for s in j.sessions:
            if s["id"] == session_id:
                return {k: v for k, v in s.items() if not k.startswith("_")}
    raise HTTPException(404, "no such session")


@app.get("/{tenant}/veeam/api/v1/backupInfrastructure/repositories/states")
def veeam_repos(tenant: str, request: Request, skip: int = 0, limit: int = 200) -> dict[str, Any]:
    e = _veeam(request, tenant)
    items = []
    for name, r in e.repositories.items():
        free = e.repository_free_gb(name)
        items.append(
            {
                "id": r["id"],
                "name": name,
                "type": "WinLocal",
                "capacityGB": r["capacity_gb"],
                "freeGB": free,
                "usedSpaceGB": r["capacity_gb"] - free,
            }
        )
    return _page(items, skip, limit)


@app.get("/{tenant}/veeam/api/v1/backups")
def veeam_backups(tenant: str, request: Request, skip: int = 0, limit: int = 200) -> dict[str, Any]:
    e = _veeam(request, tenant)
    items = [
        {
            "id": b.id,
            "name": b.name,
            "jobId": b.job_id,
            "repositoryId": e.repositories[b.repository]["id"],
            "sizeGB": b.size_gb,
            "platformName": "VMware",
        }
        for b in e.backups.values()
    ]
    return _page(items, skip, limit)


@app.post("/{tenant}/veeam/api/v1/backups/{backup_id}/removeFromDisk", status_code=202)
def veeam_remove(tenant: str, backup_id: str, request: Request) -> None:
    e = _veeam(request, tenant)
    b = e.backups.get(backup_id)
    if b is None:
        raise HTTPException(404, "no such backup")
    if b.job_id is not None:
        raise HTTPException(409, "the simulator only removes orphaned backups")
    del e.backups[backup_id]
    e.event("orphaned backup removed", backup=b.name, size_gb=b.size_gb)


# ---- Entra ID and Microsoft Graph -----------------------------------------------------------------------


@app.post("/{tenant}/login/{entra}/oauth2/v2.0/token")
async def entra_token(tenant: str, entra: str) -> dict[str, Any]:
    e = estate(tenant)
    if entra != e.entra_tenant_id:
        raise HTTPException(400, "AADSTS90002: tenant not found")
    return {"access_token": f"graph-{tenant}", "token_type": "Bearer", "expires_in": 3599}


def _graph(request: Request, tenant: str) -> model.Estate:
    if request.headers.get("authorization") != f"Bearer graph-{tenant}":
        raise HTTPException(401, "InvalidAuthenticationToken")
    return estate(tenant)


GRAPH_FIELDS = (
    "id",
    "deviceName",
    "operatingSystem",
    "osVersion",
    "complianceState",
    "lastSyncDateTime",
    "userPrincipalName",
    "serialNumber",
    "manufacturer",
    "model",
    "managementAgent",
)


def _odata_filter(items: list[dict[str, Any]], expr: str | None) -> list[dict[str, Any]]:
    if not expr:
        return items
    field, _, value = expr.partition(" eq ")
    value = value.strip().strip("'").replace("''", "'")
    return [i for i in items if str(i.get(field.strip(), "")).lower() == value.lower()]


@app.get("/{tenant}/graph/v1.0/deviceManagement/managedDevices")
def graph_devices(tenant: str, request: Request) -> dict[str, Any]:
    e = _graph(request, tenant)
    items = _odata_filter([{k: d[k] for k in GRAPH_FIELDS} for d in e.devices.values()], request.query_params.get("$filter"))
    skip = int(request.query_params.get("$skip", 0))
    page = items[skip : skip + 999]
    body: dict[str, Any] = {"value": page}
    if skip + 999 < len(items):
        body["@odata.nextLink"] = str(request.url.include_query_params(**{"$skip": skip + 999}))
    return body


@app.post("/{tenant}/graph/v1.0/deviceManagement/managedDevices/{device_id}/syncDevice", status_code=204)
def graph_sync(tenant: str, device_id: str, request: Request) -> None:
    e = _graph(request, tenant)
    dev = next((d for d in e.devices.values() if d["id"] == device_id), None)
    if dev is None:
        raise HTTPException(404, "device not found")
    dev["lastSyncDateTime"] = model._iso(time.time())
    e.event("Intune sync", device=dev["deviceName"])


@app.post(
    "/{tenant}/graph/beta/deviceManagement/managedDevices/{device_id}/initiateOnDemandProactiveRemediation", status_code=204
)
async def graph_remediate(tenant: str, device_id: str, request: Request) -> None:
    e = _graph(request, tenant)
    dev = next((d for d in e.devices.values() if d["id"] == device_id), None)
    if dev is None:
        raise HTTPException(404, "device not found")
    script = (await request.json()).get("scriptPolicyId")
    if script == "renew-vpn-certificate":
        dev["vpn_cert_days_left"] = 365
        dev["vpn_drops"] = 0
    e.event("Intune remediation", device=dev["deviceName"], script=script)


@app.get("/{tenant}/graph/v1.0/users/{upn}")
def graph_user(tenant: str, upn: str, request: Request) -> dict[str, Any]:
    e = _graph(request, tenant)
    u = e.users.get(upn.lower())
    if u is None:
        raise HTTPException(404, "Request_ResourceNotFound")
    return {k: v for k, v in u.items() if k != "locked_out"}


@app.post("/{tenant}/graph/v1.0/users/{upn}/revokeSignInSessions")
def graph_revoke(tenant: str, upn: str, request: Request) -> dict[str, Any]:
    e = _graph(request, tenant)
    if upn.lower() not in e.users:
        raise HTTPException(404, "Request_ResourceNotFound")
    e.event("sessions revoked", user=upn)
    return {"value": True}


@app.get("/{tenant}/graph/v1.0/admin/serviceAnnouncement/healthOverviews")
def graph_health(tenant: str, request: Request) -> dict[str, Any]:
    e = _graph(request, tenant)
    open_services = {i["service"] for i in e.service_issues if not i["isResolved"]}
    names = [
        "Exchange Online",
        "Microsoft Teams",
        "SharePoint Online",
        "OneDrive for Business",
        "Microsoft Entra",
        "Microsoft Intune",
    ]
    return {
        "value": [
            {
                "id": n.replace(" ", ""),
                "service": n,
                "status": "ServiceDegradation" if n in open_services else "ServiceOperational",
            }
            for n in names
        ]
    }


@app.get("/{tenant}/graph/v1.0/admin/serviceAnnouncement/issues")
def graph_issues(tenant: str, request: Request) -> dict[str, Any]:
    return {"value": _graph(request, tenant).service_issues}


# ---- Fleet -----------------------------------------------------------------------------------------------


def _fleet(request: Request, tenant: str) -> model.Estate:
    _bearer(request)
    return estate(tenant)


def _fleet_host(i: int, d: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": i,
        "hostname": d["deviceName"],
        "display_name": d["deviceName"],
        "platform": "darwin" if d["operatingSystem"] == "macOS" else "windows",
        "os_version": f"{d['operatingSystem']} {d['osVersion']}",
        "hardware_serial": d["serialNumber"],
        "status": "online",
        "team_id": 1,
    }


@app.get("/{tenant}/fleet/api/v1/fleet/hosts")
def fleet_hosts(tenant: str, request: Request, page: int = 0, per_page: int = 500) -> dict[str, Any]:
    e = _fleet(request, tenant)
    hosts = [_fleet_host(i, d) for i, d in enumerate(e.devices.values(), 1)]
    return {"hosts": hosts[page * per_page : (page + 1) * per_page]}


@app.get("/{tenant}/fleet/api/v1/fleet/hosts/identifier/{name}")
def fleet_host(tenant: str, name: str, request: Request) -> dict[str, Any]:
    e = _fleet(request, tenant)
    for i, d in enumerate(e.devices.values(), 1):
        if d["deviceName"].lower() == name.lower():
            return {"host": _fleet_host(i, d)}
    raise HTTPException(404, "host not found")


@app.post("/{tenant}/fleet/api/v1/fleet/hosts/{host_id}/query")
async def fleet_query(tenant: str, host_id: int, request: Request) -> dict[str, Any]:
    e = _fleet(request, tenant)
    d = list(e.devices.values())[host_id - 1]
    sql = (await request.json())["query"]
    if "certificates" in sql:
        days = d["vpn_cert_days_left"]
        rows = [
            {
                "common_name": f"vpn-{d['deviceName'].lower()}",
                "not_valid_after": int(time.time() + days * 86400),
                "issuer": f"{e.name} Issuing CA",
            }
        ]
    elif "mounts" in sql:
        rows = [{"path": "C:\\", "free_pct": 41.0}]
    elif "uptime" in sql:
        rows = [{"days": 3, "hours": 4}]
    else:
        rows = []
    return {"host_id": host_id, "query": sql, "status": 0, "error": None, "rows": rows}


# ---- edge node -------------------------------------------------------------------------------------------


@app.post("/{tenant}/edge/v1/ops/{op}")
async def edge_op(tenant: str, op: str, request: Request) -> dict[str, Any]:
    _bearer(request)
    e = estate(tenant)
    args = await request.json()

    def vm() -> model.VM:
        v = e.vms.get(str(args.get("host", "")).upper())
        if v is None:
            raise HTTPException(404, f"no server {args.get('host')}")
        return v

    try:
        if op == "service_status":
            v = vm()
            return {
                "ok": True,
                "result": {"host": v.name, "service": args["service"], "state": v.services.get(args["service"], "not installed")},
            }
        if op == "restart_service":
            v = vm()
            if args["service"] not in v.services:
                return {"ok": False, "error": f"{args['service']} is not installed on {v.name}"}
            v.services[args["service"]] = "running"
            e.event("service restarted", host=v.name, service=args["service"])
            return {"ok": True, "result": {"host": v.name, "service": args["service"], "state": "running"}}
        if op == "disk_usage":
            v = vm()
            used = model.disk_used_now(e, v)
            return {
                "ok": True,
                "result": {
                    "host": v.name,
                    "volumes": [
                        {
                            "mount": "C:",
                            "size_gb": v.disk_gb,
                            "used_ratio": round(used, 3),
                            "free_gb": round(v.disk_gb * (1 - used), 1),
                        }
                    ],
                },
            }
        if op == "cleanup_disk":
            v = vm()
            before = model.disk_used_now(e, v)
            v.disk_used = max(0.3, round(before - 0.12, 3))
            if "cert-and-disk-forecast" in e.faults and v.name == "CTO-FS01":
                e.faults.pop("cert-and-disk-forecast", None)
            e.event("disk cleaned", host=v.name, freed_gb=round((before - v.disk_used) * v.disk_gb, 1))
            return {"ok": True, "result": {"host": v.name, "freed_gb": round((before - v.disk_used) * v.disk_gb, 1)}}
        if op == "certificates":
            v = vm()
            return {"ok": True, "result": {"host": v.name, "certificates": v.certificates}}
        if op == "renew_certificate":
            v = vm()
            cert = next((c for c in v.certificates if c["subject"] == args["certificate"]), None)
            if cert is None:
                return {"ok": False, "error": "no such certificate"}
            cert["days_left"] = 397
            e.event("certificate renewed", host=v.name, certificate=cert["subject"])
            return {"ok": True, "result": cert}
        if op == "network_devices":
            return {"ok": True, "result": {"devices": list(e.network.values())}}
        if op == "firewall_path":
            return {
                "ok": True,
                "result": {
                    "allowed": True,
                    "rule": "VPN-USERS-TO-LAN",
                    "firewall": next(n for n in e.network if n.endswith("FW-01")),
                },
            }
        if op in ("account_status", "unlock_account"):
            user = e.users.get(str(args.get("username", "")).lower())
            if user is None:
                return {"ok": False, "error": "no such account"}
            if op == "unlock_account":
                user["locked_out"] = False
                e.event("account unlocked", user=user["userPrincipalName"])
            return {
                "ok": True,
                "result": {
                    "username": user["userPrincipalName"],
                    "locked_out": user["locked_out"],
                    "enabled": user["accountEnabled"],
                },
            }
    except KeyError as exc:
        return {"ok": False, "error": f"missing argument {exc}"}
    raise HTTPException(404, f"unknown operation {op}")


# ---- telemetry -------------------------------------------------------------------------------------------


def _line(name: str, labels: dict[str, Any], value: float) -> str:
    text = ",".join(f'{k}="{str(v).replace(chr(34), "")}"' for k, v in labels.items())
    return f"{name}{{{text}}} {value}"


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    lines: list[str] = []
    for t, e in ESTATES.items():
        _finish_running(e)
        for vm in e.vms.values():
            base = {"tenant_id": t, "host": vm.name}
            lines.append(_line("host_cpu_utilization", base, round(min(1.0, max(0.0, vm.cpu + model.noise(vm.name, 0.03))), 4)))
            lines.append(_line("host_memory_utilization", base, round(vm.memory, 4)))
            lines.append(_line("host_disk_used_ratio", base | {"mount": "C:"}, round(model.disk_used_now(e, vm), 4)))
            lines.append(
                _line("host_disk_latency_ms", base, round(e.vm_disk_latency(vm) + abs(model.noise(vm.name + "l", 0.3)), 2))
            )
            lines.append(_line("host_disk_iops", base, round(vm.io_load * (1 + model.noise(vm.name + "io", 0.05)))))
            for svc, state in vm.services.items():
                lines.append(_line("windows_service_up", base | {"win_service": svc}, 1 if state == "running" else 0))
            for c in vm.certificates:
                lines.append(_line("certificate_days_left", base | {"certificate": c["subject"]}, c["days_left"]))
        for ds in e.datastores:
            lines.append(
                _line(
                    "datastore_latency_ms",
                    {"tenant_id": t, "datastore": ds},
                    round(e.datastore_latency(ds) + abs(model.noise(ds, 0.2)), 2),
                )
            )
            d = e.datastores[ds]
            lines.append(_line("datastore_used_ratio", {"tenant_id": t, "datastore": ds}, round(d.used_gb / d.capacity_gb, 4)))
        for h, info in e.hosts.items():
            load = sum(v.cpu * v.vcpus for v in e.vms.values() if v.host == h) / 64
            lines.append(
                _line(
                    "hypervisor_cpu_utilization",
                    {"tenant_id": t, "hypervisor": h, "cluster": info["cluster"]},
                    round(min(1.0, load), 4),
                )
            )
        for s in e.services:
            lines.append(
                _line(
                    "service_latency_p95_ms", {"tenant_id": t, "service": s}, round(e.service_latency(s) + model.noise(s, 8), 1)
                )
            )
            lines.append(_line("service_error_ratio", {"tenant_id": t, "service": s}, e.service_errors(s)))
        for r in e.repositories:
            lines.append(_line("backup_repository_free_ratio", {"tenant_id": t, "repository": r}, e.repository_free(r)))
        for d in e.devices.values():
            if d["vpn_drops"]:
                lines.append(
                    _line(
                        "vpn_tunnel_drops_total",
                        {"tenant_id": t, "device": d["deviceName"]},
                        d["vpn_drops"] * (1 + int(time.time() // 600) % 50),
                    )
                )
    return "\n".join(lines) + "\n"


def _log_lines(e: model.Estate) -> list[tuple[str, str, str]]:
    """(host, severity, message) for what is going wrong right now."""
    out = []
    if "checkout-slow" in e.faults:
        out += [
            ("CTO-SHOP-API01", "ERROR", "Timeout after 5000 ms waiting for SQL connection to CTO-SHOP-SQL01"),
            (
                "CTO-SHOP-SQL01",
                "WARN",
                "SQL Server has encountered 37 occurrence(s) of I/O requests taking longer than 15 seconds "
                "to complete on file [D:\\Data\\shop.mdf]",
            ),
            ("CTO-BI-ETL01", "INFO", "ETL job SalesHistoryRebuild running full reload: 1.2 billion rows, no throttle configured"),
            ("CTO-SHOP-WEB02", "ERROR", "HTTP 504 from checkout API after 8000 ms"),
        ]
    if "backup-repo-full" in e.faults:
        out += [("FAB-VBR01", "ERROR", "Job EHR SQL nightly failed: Insufficient free space on repository FAB-REPO-01")]
    if "vpn-cert-expired" in e.faults and e.devices["LT-CTO-0231"]["vpn_cert_days_left"] < 0:
        out += [("CTO-FW-01", "WARN", "IKE negotiation failed for user priya.nair@contoso.example: peer certificate expired")]
    for v in e.vms.values():
        if v.services.get("Spooler") == "stopped":
            out.append((v.name, "ERROR", "The Print Spooler service terminated unexpectedly. It has done this 3 time(s)."))
    return out


async def _ship_logs() -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                resource_logs = []
                for t, e in ESTATES.items():
                    by_host: dict[str, list[dict[str, Any]]] = {}
                    for host, sev, msg in _log_lines(e):
                        by_host.setdefault(host, []).append(
                            {"timeUnixNano": str(time.time_ns()), "severityText": sev, "body": {"stringValue": msg}}
                        )
                    for host, records in by_host.items():
                        attrs = [
                            {"key": "tenant_id", "value": {"stringValue": t}},
                            {"key": "host", "value": {"stringValue": host}},
                            {"key": "service.name", "value": {"stringValue": "estate"}},
                        ]
                        resource_logs.append({"resource": {"attributes": attrs}, "scopeLogs": [{"logRecords": records}]})
                if resource_logs:
                    await client.post(
                        OTLP_LOGS_URL,
                        content=json.dumps({"resourceLogs": resource_logs}),
                        headers={"Content-Type": "application/json"},
                    )
            except Exception as exc:  # the collector may not be up yet
                log.warning("shipping logs failed: %s", exc)
            await asyncio.sleep(15)


# ---- control ---------------------------------------------------------------------------------------------


@app.get("/_sim/faults")
def faults() -> list[dict[str, Any]]:
    return [{"id": k, **v, "active": k in ESTATES[v["tenant"]].faults} for k, v in model.FAULTS.items()]


@app.post("/_sim/faults/{fault}")
def inject(fault: str) -> dict[str, Any]:
    spec = model.FAULTS.get(fault)
    if spec is None:
        raise HTTPException(404, "no such fault")
    e = ESTATES[spec["tenant"]]
    model.inject(e, fault)
    e.event("fault injected", fault=fault)
    return {"ok": True, "fault": fault}


@app.post("/_sim/reset")
def reset() -> dict[str, Any]:
    ESTATES.clear()
    ESTATES.update(model.build_all())
    return {"ok": True}


@app.get("/_sim/events")
def events() -> list[dict[str, Any]]:
    return sorted((ev | {"tenant": t} for t, e in ESTATES.items() for ev in e.events), key=lambda x: x["at"], reverse=True)


@app.get("/_sim/declared/{tenant}")
def declared(tenant: str) -> dict[str, Any]:
    """The facts discovery can't infer (business services, owners, criticality), as the customer declares them."""
    e = estate(tenant)
    return {
        "services": [
            {"name": s["name"], "owner": s["owner"], "critical": s["critical"], "users": s["users"], "servers": s["vms"]}
            for s in e.services.values()
        ]
    }
