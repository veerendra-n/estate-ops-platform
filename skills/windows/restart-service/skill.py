"""Restart a stopped Windows service through the edge node."""

from __future__ import annotations

from typing import Any


def gather(ctx) -> dict[str, Any]:
    status = ctx.connector("edge").op("service_status", host=ctx.target, service=ctx.params["service"])
    return {"host": ctx.target, "service": ctx.params["service"], "state": status.get("state")}


def decide(facts: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    state = facts["state"]
    if state == "running":
        return {"action": "none", "reason": f"{facts['service']} is running on {facts['host']}", "plan": []}
    if state in (None, "not installed"):
        return {"action": "escalate", "reason": f"{facts['service']} is not installed on {facts['host']}", "plan": []}
    return {
        "action": "restart_service",
        "reason": f"{facts['service']} is {state} on {facts['host']}",
        "plan": [{"host": facts["host"], "service": facts["service"]}],
    }


def act(ctx, facts: dict[str, Any], decision: dict[str, Any]) -> list[dict[str, Any]]:
    edge = ctx.connector("edge")
    return [{"did": "restart_service", **edge.op("restart_service", **step)} for step in decision["plan"]]


def verify(ctx, facts: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    ctx.sleep(ctx.params["verify_seconds"])
    state = ctx.connector("edge").op("service_status", host=facts["host"], service=facts["service"]).get("state")
    return {
        "ok": state == "running",
        "state": state,
        "reason": None if state == "running" else f"{facts['service']} is {state} again",
    }
