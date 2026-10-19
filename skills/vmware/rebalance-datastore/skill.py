"""Move the noisiest non-critical VM off a slow datastore to the one with the most headroom."""

from __future__ import annotations

import re
import time
from typing import Any

MIN_FREE_GB = 200


def _datastore_of(vm: dict[str, Any]) -> str | None:
    """The datastore of a VM's first disk, from its vmdk path "[DS-NAME] folder/file.vmdk"."""
    for disk in (vm.get("disks") or {}).values():
        m = re.match(r"^\[([^\]]+)\]", (disk.get("backing") or {}).get("vmdk_file", ""))
        if m:
            return m.group(1)
    return None


def gather(ctx) -> dict[str, Any]:
    vc, metrics = ctx.connector("vcenter"), ctx.connector("metrics")
    stores = {d["name"]: d for d in vc.datastores()}
    if ctx.target not in stores:
        raise LookupError(f"vCenter has no datastore {ctx.target}")
    here = stores[ctx.target]
    vms = []
    for vm in vc.vms(datastore=here["datastore"]):
        if vm.get("power_state") != "POWERED_ON":
            continue
        radius = ctx.graph.blast_radius(ctx.tenant.id, [f"server:{vm['name']}"]) if ctx.graph else {}
        vms.append(
            {
                "name": vm["name"],
                "id": vm["vm"],
                "iops": metrics.latest("host_disk_iops", vm["name"]) or 0.0,
                "critical": bool(radius.get("critical")),
                "services": radius.get("services", []),
            }
        )
    others = []
    for name, d in stores.items():
        if name == ctx.target or "NONPROD" in name.upper():
            continue
        radius = ctx.graph.blast_radius(ctx.tenant.id, [f"datastore:{name}"]) if ctx.graph else {}
        others.append(
            {
                "name": name,
                "id": d["datastore"],
                "latency_ms": metrics.latest("datastore_latency_ms", name),
                "free_gb": round(d["free_space"] / 1024**3),
                # Never park a noisy VM next to a critical service's VMs.
                "critical": bool(radius.get("critical")),
            }
        )
    return {
        "datastore": ctx.target,
        "datastore_id": here["datastore"],
        "latency_ms": metrics.latest("datastore_latency_ms", ctx.target),
        "vms": sorted(vms, key=lambda v: -v["iops"]),
        "candidates": others,
    }


def decide(facts: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    latency = facts.get("latency_ms")
    if latency is None:
        return {"action": "escalate", "reason": f"no latency reading for {facts['datastore']}", "plan": []}
    if latency < params["latency_threshold_ms"]:
        return {
            "action": "none",
            "reason": f"{facts['datastore']} is at {latency:.1f} ms, under {params['latency_threshold_ms']} ms",
            "plan": [],
        }
    total = sum(v["iops"] for v in facts["vms"]) or 1.0
    movable = [v for v in facts["vms"] if not v["critical"]]
    if not movable:
        return {"action": "escalate", "reason": "every VM on the datastore belongs to a critical service", "plan": []}
    top = movable[0]
    share = top["iops"] / total
    if share < params["min_share"]:
        return {
            "action": "escalate",
            "reason": f"the busiest movable VM, {top['name']}, drives only {share:.0%} of the IO; no single VM explains the load",
            "plan": [],
        }
    fits = [
        c
        for c in facts["candidates"]
        if not c.get("critical")
        and c["latency_ms"] is not None
        and c["latency_ms"] <= params["max_destination_latency_ms"]
        and c["free_gb"] >= MIN_FREE_GB
    ]
    if not fits:
        return {"action": "escalate", "reason": "no other non-critical production datastore has the headroom", "plan": []}
    dest = sorted(fits, key=lambda c: (c["latency_ms"], -c["free_gb"]))[0]
    return {
        "action": "storage_vmotion",
        "reason": (
            f"{top['name']} drives {share:.0%} of the IO on {facts['datastore']} ({latency:.1f} ms) and no critical service "
            f"runs on it; {dest['name']} is at {dest['latency_ms']:.1f} ms with {dest['free_gb']} GB free"
        ),
        "plan": [{"vm": top["name"], "vm_id": top["id"], "from": facts["datastore"], "to": dest["name"], "to_id": dest["id"]}],
    }


def act(ctx, facts: dict[str, Any], decision: dict[str, Any]) -> list[dict[str, Any]]:
    vc = ctx.connector("vcenter")
    done = []
    for step in decision["plan"]:
        vc.relocate(step["vm_id"], datastore=step["to_id"])
        ctx.note(f"storage vMotion of {step['vm']} from {step['from']} to {step['to']} requested")
        done.append({"did": "storage_vmotion", **step})
    return done


def verify(ctx, facts: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    vc, metrics = ctx.connector("vcenter"), ctx.connector("metrics")
    step = decision["plan"][0]
    placed = _datastore_of(vc.vm(step["vm_id"]))
    if placed != step["to"]:
        return {"ok": False, "reason": f"{step['vm']} is on {placed}, not {step['to']}"}
    deadline = time.monotonic() + ctx.params["verify_minutes"] * 60
    latency = None
    while True:
        latency = metrics.latest("datastore_latency_ms", facts["datastore"])
        if latency is not None and latency < ctx.params["latency_threshold_ms"]:
            moved_to = metrics.latest("datastore_latency_ms", step["to"])
            if moved_to is not None and moved_to >= ctx.params["latency_threshold_ms"]:
                return {
                    "ok": False,
                    "vm_on": placed,
                    "latency_ms": latency,
                    "destination_latency_ms": moved_to,
                    "reason": f"{step['to']} is now at {moved_to} ms; move {step['vm']} back (see rollback)",
                }
            return {"ok": True, "vm_on": placed, "latency_ms": latency, "destination_latency_ms": moved_to}
        if time.monotonic() >= deadline:
            return {
                "ok": False,
                "vm_on": placed,
                "latency_ms": latency,
                "reason": f"{facts['datastore']} is still at {latency} ms",
            }
        ctx.sleep(15)
