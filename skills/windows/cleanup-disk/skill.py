"""Clean up a nearly full Windows volume through the edge node."""

from __future__ import annotations

from typing import Any


def _volume(usage: dict[str, Any], mount: str) -> dict[str, Any] | None:
    return next((v for v in usage.get("volumes", []) if v["mount"].upper() == mount.upper()), None)


def gather(ctx) -> dict[str, Any]:
    vol = _volume(ctx.connector("edge").op("disk_usage", host=ctx.target), ctx.params["mount"])
    return {"host": ctx.target, "mount": ctx.params["mount"], "volume": vol}


def decide(facts: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    vol = facts["volume"]
    if vol is None:
        return {"action": "escalate", "reason": f"{facts['host']} has no volume {facts['mount']}", "plan": []}
    if vol["used_ratio"] < params["threshold"]:
        return {"action": "none", "reason": f"{facts['mount']} is {vol['used_ratio']:.0%} used", "plan": []}
    return {
        "action": "cleanup_disk",
        "reason": f"{facts['mount']} on {facts['host']} is {vol['used_ratio']:.0%} used ({vol['free_gb']} GB free)",
        "plan": [{"host": facts["host"], "mount": facts["mount"]}],
    }


def act(ctx, facts: dict[str, Any], decision: dict[str, Any]) -> list[dict[str, Any]]:
    edge = ctx.connector("edge")
    return [{"did": "cleanup_disk", **edge.op("cleanup_disk", **step)} for step in decision["plan"]]


def verify(ctx, facts: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    vol = _volume(ctx.connector("edge").op("disk_usage", host=facts["host"]), facts["mount"]) or {}
    ok = vol.get("used_ratio", 1) < ctx.params["threshold"]
    return {
        "ok": ok,
        "used_ratio": vol.get("used_ratio"),
        "reason": None if ok else "still over the threshold; the disk needs to grow",
    }
