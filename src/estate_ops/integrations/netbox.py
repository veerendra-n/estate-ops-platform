"""Discovery from NetBox into the estate graph.

NetBox is the inventory source of truth (ops-foundation onboards each customer as a NetBox tenant with
the same id). Discovery reads that tenant's devices, virtual machines and clusters and writes them to
Neo4j with source "netbox", replacing what the previous run wrote. Applications, databases and backup
jobs come from other sources (the seed file today), which this leaves alone.

Asset ids match the seed and the alerts: host:<name> for servers and VMs, so an alert on lab-sql01 finds
the same node whether it came from the seed or from NetBox.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..estate import EstateGraph
from ..tenancy import Tenant

SOURCE = "netbox"
# Device role slugs (substring match) to estate graph kinds. Anything else is a "device".
ROLE_KINDS = (
    ("server", "server"),
    ("hypervisor", "hypervisor_host"),
    ("host", "hypervisor_host"),
    ("switch", "network_device"),
    ("router", "network_device"),
    ("firewall", "network_device"),
    ("access-point", "network_device"),
    ("wireless", "network_device"),
    ("storage", "storage_array"),
    ("nas", "storage_array"),
    ("san", "storage_array"),
    ("ups", "power"),
    ("pdu", "power"),
)


def _slug(obj: dict[str, Any] | None) -> str | None:
    return (obj or {}).get("slug") or (obj or {}).get("name")


def device_kind(device: dict[str, Any]) -> str:
    role = (_slug(device.get("role") or device.get("device_role")) or "").lower()
    if device.get("cluster"):
        return "hypervisor_host"
    for needle, kind in ROLE_KINDS:
        if needle in role:
            return kind
    return "device"


def _attrs(obj: dict[str, Any], **extra: Any) -> dict[str, Any]:
    attrs = {
        "netbox_id": obj.get("id"),
        "status": (obj.get("status") or {}).get("value"),
        "role": _slug(obj.get("role") or obj.get("device_role")),
        "platform": _slug(obj.get("platform")),
        "site": _slug(obj.get("site")),
        "ip": ((obj.get("primary_ip") or {}).get("address") or "").split("/")[0] or None,
        **extra,
    }
    return {k: v for k, v in attrs.items() if v not in (None, "")}


class NetBoxClient:
    def __init__(self, url: str, token: str, client: httpx.Client | None = None):
        self.url = url.rstrip("/")
        # NetBox 4.5+ v2 tokens (nbt_...) use Bearer; older v1 tokens use the Token scheme.
        scheme = "Bearer" if token.startswith("nbt_") else "Token"
        self.http = client or httpx.Client(timeout=60)
        self.headers = {"Authorization": f"{scheme} {token}", "Accept": "application/json"}

    def list(self, path: str, **params: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        url: str | None = f"{self.url}/api/{path.strip('/')}/"
        query: dict[str, Any] | None = {"limit": 500, **params}
        while url:
            r = self.http.get(url, params=query, headers=self.headers)
            r.raise_for_status()
            page = r.json()
            out.extend(page["results"])
            url, query = page.get("next"), None  # "next" already carries the query
        return out


def collect(client: NetBoxClient, tenant: Tenant) -> tuple[list[dict], list[list[str]]]:
    slug = tenant.netbox_tenant or tenant.id
    devices = client.list("dcim/devices", tenant=slug)
    vms = client.list("virtualization/virtual-machines", tenant=slug)
    clusters = client.list("virtualization/clusters", tenant=slug)
    assets: dict[str, dict] = {}
    relations: list[list[str]] = []

    for c in clusters:
        assets[f"cluster:{c['name']}"] = {
            "id": f"cluster:{c['name']}",
            "kind": "hypervisor_cluster",
            "name": c["name"],
            "attrs": _attrs(c, type=_slug(c.get("type"))),
        }
    for d in devices:
        if not d.get("name"):
            continue  # unnamed patch panels and the like
        kind = device_kind(d)
        prefix = {"server": "host", "hypervisor_host": "hv", "network_device": "net", "storage_array": "storage"}.get(kind, "dev")
        aid = f"{prefix}:{d['name']}"
        assets[aid] = {"id": aid, "kind": kind, "name": d["name"], "attrs": _attrs(d)}
        if d.get("cluster"):
            relations.append([aid, "member_of", f"cluster:{d['cluster']['name']}"])
    for vm in vms:
        aid = f"host:{vm['name']}"
        assets[aid] = {
            "id": aid,
            "kind": "server",
            "name": vm["name"],
            "attrs": _attrs(vm, vcpus=vm.get("vcpus"), memory_mb=vm.get("memory"), virtual=True),
        }
        if vm.get("device"):  # pinned to a host
            relations.append([aid, "runs_on", f"hv:{vm['device']['name']}"])
        elif vm.get("cluster"):
            relations.append([aid, "runs_on", f"cluster:{vm['cluster']['name']}"])
    known = set(assets)
    return list(assets.values()), [r for r in relations if r[0] in known and r[2] in known]


def discover(client: NetBoxClient, graph: EstateGraph, tenant: Tenant) -> dict[str, int]:
    assets, relations = collect(client, tenant)
    graph.drop_relations(tenant.id, SOURCE)
    graph.upsert(tenant.id, SOURCE, assets, relations)
    removed = graph.prune(tenant.id, SOURCE, [a["id"] for a in assets])
    return {"assets": len(assets), "relations": len(relations), "removed": removed}
