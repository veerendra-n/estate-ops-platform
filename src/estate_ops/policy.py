"""The autonomy dial, decided by Open Policy Agent (policy/autonomy.rego) before every skill run.

This module builds the policy input from what the platform knows (the customer's chosen autonomy and
change freezes, the skill's risk, the blast radius from the estate graph, the skill's recent record at
this customer) and asks OPA. If OPA can't be reached, the answer is "recommend": nothing runs on an
estate without a policy decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from .skills import Skill
from .tenancy import Tenant

MODES = ("recommend", "approve_to_run", "run_and_notify")


@dataclass(frozen=True)
class Decision:
    mode: str
    reason: str
    wanted: str = ""
    caps: list[str] = field(default_factory=list)
    input: dict[str, Any] = field(default_factory=dict, compare=False)


def build_input(
    tenant: Tenant,
    skill: Skill,
    targets: list[str],
    blast_radius: dict[str, Any],
    history: dict[str, int],
    now: datetime | None = None,
) -> dict[str, Any]:
    return {
        "tenant": {
            "id": tenant.id,
            "autonomy": dict(tenant.autonomy),
            "change_freeze": list(tenant.contract.get("change_freeze") or []),
        },
        "skill": {"id": skill.id, "risk": skill.risk, "approval": skill.approval, "max_targets": skill.max_targets},
        "targets": targets,
        "blast_radius": {
            "dependents": int(blast_radius.get("dependents", 0)),
            "services": list(blast_radius.get("services") or []),
            "critical": bool(blast_radius.get("critical")),
        },
        "history": {"live_runs_30d": int(history.get("live_runs_30d", 0)), "failures_30d": int(history.get("failures_30d", 0))},
        "now": (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
    }


class Policy:
    def __init__(self, url: str, client: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.http = client or httpx.Client(timeout=5)

    def decide(self, policy_input: dict[str, Any]) -> Decision:
        try:
            r = self.http.post(f"{self.url}/v1/data/estate/autonomy/decision", json={"input": policy_input})
            r.raise_for_status()
            result = r.json().get("result")
        except httpx.HTTPError as exc:
            return Decision("recommend", f"the policy service could not be reached ({type(exc).__name__})", input=policy_input)
        if not isinstance(result, dict) or result.get("mode") not in MODES:
            return Decision("recommend", "the policy returned no decision", input=policy_input)
        return Decision(result["mode"], result["reason"], result.get("wanted", ""), list(result.get("caps") or []), policy_input)
