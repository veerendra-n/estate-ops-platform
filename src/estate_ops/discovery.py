"""Discovery: each customer's systems read into the estate graph, and what changed since the last read.

Each collector turns one connector's view into (assets, relations) with asset ids "<kind>:<name>":

    vcenter   hypervisor_cluster, hypervisor_host, datastore, server (VMs), with runs_on, stored_on, member_of
    veeam     backup_job -protects-> server, backup_job -writes_to-> backup_repository
    intune    endpoint (laptops and desktops, with their user and compliance)
    m365      m365_service (Microsoft 365 workloads and their health)
    network   network_device (switches, firewalls) from the edge node
    declared  service -runs_on-> server: business services, owners and criticality the customer declares in
              config/estate/<tenant>.yml, because no API knows which servers make up "Checkout"

Runs in this order so relations find both ends. A run diffs the source's view against its previous one and
writes graph_changes, which agents read as "what changed before it broke".
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from .connectors import Connectors
from .tenancy import Tenant

Assets = list[dict[str, Any]]
Relations = list[list[str]]

# Attributes that move on their own every read; a change in these is not news.
VOLATILE = {"free_gb", "last_sync", "status", "used_ratio"}
GB = 1024**3


def _datastore_of(vm: dict[str, Any]) -> str | None:
    for disk in (vm.get("disks") or {}).values():
        m = re.match(r"^\[([^\]]+)\]", (disk.get("backing") or {}).get("vmdk_file", ""))
        if m:
            return m.group(1)
    return None


def vcenter(conns: Connectors) -> tuple[Assets, Relations]:
    vc = conns.vcenter()
    try:
        assets: Assets = []
        relations: Relations = []
        host_names: dict[str, str] = {}
        for c in vc.clusters():
            cid = f"hypervisor_cluster:{c['name']}"
            assets.append(
                {
                    "id": cid,
                    "kind": "hypervisor_cluster",
                    "name": c["name"],
                    "attrs": {"ha": c.get("ha_enabled"), "drs": c.get("drs_enabled")},
                }
            )
            for h in vc.hosts(c["cluster"]):
                host_names[h["host"]] = h["name"]
                assets.append(
                    {
                        "id": f"hypervisor_host:{h['name']}",
                        "kind": "hypervisor_host",
                        "name": h["name"],
                        "attrs": {
                            "cluster": c["name"],
                            "connection_state": h.get("connection_state"),
                            "power_state": h.get("power_state"),
                        },
                    }
                )
                relations.append([f"hypervisor_host:{h['name']}", "member_of", cid])
        vm_attrs: dict[str, dict[str, Any]] = {}
        for host_id, host_name in host_names.items():
            for vm in vc.vms(host=host_id):
                vm_attrs[vm["name"]] = {
                    "power_state": vm.get("power_state"),
                    "cpu_count": vm.get("cpu_count"),
                    "memory_gb": round((vm.get("memory_size_MiB") or 0) / 1024),
                    "host": host_name,
                }
                relations.append([f"server:{vm['name']}", "runs_on", f"hypervisor_host:{host_name}"])
        for d in vc.datastores():
            assets.append(
                {
                    "id": f"datastore:{d['name']}",
                    "kind": "datastore",
                    "name": d["name"],
                    "attrs": {
                        "type": d.get("type"),
                        "capacity_gb": round(d["capacity"] / GB),
                        "free_gb": round(d["free_space"] / GB),
                    },
                }
            )
            for vm in vc.vms(datastore=d["datastore"]):
                vm_attrs.setdefault(vm["name"], {})["datastore"] = d["name"]
                relations.append([f"server:{vm['name']}", "stored_on", f"datastore:{d['name']}"])
        assets += [
            {"id": f"server:{n}", "kind": "server", "name": n, "attrs": a | {"platform": "vmware"}} for n, a in vm_attrs.items()
        ]
        return assets, relations
    finally:
        vc.close()


def veeam(conns: Connectors) -> tuple[Assets, Relations]:
    client = conns.veeam()
    repos = {r["id"]: r for r in client.repositories()}
    assets: Assets = [
        {
            "id": f"backup_repository:{r['name']}",
            "kind": "backup_repository",
            "name": r["name"],
            "attrs": {"capacity_gb": r.get("capacityGB"), "free_gb": r.get("freeGB"), "type": r.get("type")},
        }
        for r in repos.values()
    ]
    relations: Relations = []
    for job in client.jobs():
        jid = f"backup_job:{job['name']}"
        repo = repos.get((job.get("storage") or {}).get("backupRepositoryId"))
        assets.append(
            {
                "id": jid,
                "kind": "backup_job",
                "name": job["name"],
                "attrs": {"type": job.get("type"), "disabled": job.get("isDisabled")},
            }
        )
        if repo:
            relations.append([jid, "writes_to", f"backup_repository:{repo['name']}"])
        for vm in (job.get("virtualMachines") or {}).get("includes", []):
            relations.append([jid, "protects", f"server:{vm['name']}"])
    return assets, relations


def intune(conns: Connectors) -> tuple[Assets, Relations]:
    assets = [
        {
            "id": f"endpoint:{d['deviceName']}",
            "kind": "endpoint",
            "name": d["deviceName"],
            "attrs": {
                "os": d.get("operatingSystem"),
                "os_version": d.get("osVersion"),
                "compliance": d.get("complianceState"),
                "user": (d.get("userPrincipalName") or "").lower(),
                "model": d.get("model"),
                "manufacturer": d.get("manufacturer"),
                "last_sync": d.get("lastSyncDateTime"),
            },
        }
        for d in conns.microsoft().devices()
    ]
    return assets, []


def m365(conns: Connectors) -> tuple[Assets, Relations]:
    return [
        {"id": f"m365_service:{s['service']}", "kind": "m365_service", "name": s["service"], "attrs": {"status": s.get("status")}}
        for s in conns.microsoft().service_health()
    ], []


def network(conns: Connectors) -> tuple[Assets, Relations]:
    devices = conns.edge().op("network_devices").get("devices", [])
    return [
        {
            "id": f"network_device:{d['name']}",
            "kind": "network_device",
            "name": d["name"],
            "attrs": {"role": d.get("kind"), "model": d.get("model"), "version": d.get("version"), "site": d.get("site")},
        }
        for d in devices
    ], []


def declared(tenant: Tenant, directory: Path) -> tuple[Assets, Relations]:
    path = directory / (tenant.declared_file or f"{tenant.id}.yml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assets: Assets = []
    relations: Relations = []
    for s in raw.get("services", []):
        sid = f"service:{s['name']}"
        attrs = {k: s.get(k) for k in ("owner", "critical", "users", "hours", "description") if s.get(k) is not None}
        assets.append({"id": sid, "kind": "service", "name": s["name"], "attrs": attrs})
        relations += [[sid, "runs_on", f"server:{name}"] for name in s.get("servers", [])]
    return assets, relations


def collectors(settings) -> list[tuple[str, str | None, Callable[[Tenant, Connectors], tuple[Assets, Relations]]]]:
    """(source name, connector it needs or None, collector), in the order they run."""
    return [
        ("vcenter", "vcenter", lambda t, c: vcenter(c)),
        ("veeam", "veeam", lambda t, c: veeam(c)),
        ("intune", "microsoft", lambda t, c: intune(c)),
        ("m365", "microsoft", lambda t, c: m365(c)),
        ("network", "edge", lambda t, c: network(c)),
        ("declared", None, lambda t, c: declared(t, settings.declared_dir)),
    ]


def diff(source: str, before: dict[str, dict[str, Any]], assets: Assets) -> list[dict[str, Any]]:
    """What changed in one source's view. The first read of a source is not a change."""
    if not before:
        return []
    out = []
    now_ids = set()
    for a in assets:
        now_ids.add(a["id"])
        old = before.get(a["id"])
        new_attrs = {k: v for k, v in (a.get("attrs") or {}).items() if k not in VOLATILE and v is not None}
        if old is None:
            out.append(
                {
                    "source": source,
                    "asset_id": a["id"],
                    "asset_name": a["name"],
                    "change": "added",
                    "before": None,
                    "after": new_attrs,
                }
            )
            continue
        was = {k: old["attrs"].get(k) for k in new_attrs}
        moved = {k: v for k, v in new_attrs.items() if was.get(k) != v}
        if moved:
            out.append(
                {
                    "source": source,
                    "asset_id": a["id"],
                    "asset_name": a["name"],
                    "change": "changed",
                    "before": {k: was.get(k) for k in moved},
                    "after": moved,
                }
            )
    for asset_id, old in before.items():
        if asset_id not in now_ids:
            out.append(
                {
                    "source": source,
                    "asset_id": asset_id,
                    "asset_name": old["name"],
                    "change": "removed",
                    "before": old["attrs"],
                    "after": None,
                }
            )
    return out
