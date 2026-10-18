"""The autonomy dial: how far the platform may go with a runbook for a tenant.

Plain code for now, so every rule has a unit test. If rules multiply across customers, move them to
Open Policy Agent; the inputs and outputs here are already shaped for that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .tenancy import Tenant

ORDER = ("recommend", "approve_to_run", "run_and_notify")


@dataclass(frozen=True)
class Decision:
    mode: str
    reason: str


def decide(tenant: Tenant, runbook: dict[str, Any], targets: list[str]) -> Decision:
    wanted = tenant.autonomy_for(runbook["id"])
    if len(targets) > int(runbook.get("max_targets", 1)):
        return Decision(
            "recommend", f"{len(targets)} targets is more than this runbook's limit of {runbook.get('max_targets', 1)}"
        )
    if runbook["risk"] == "high":
        return Decision("recommend", "high-risk runbooks are not run by agents in this phase")
    if runbook["approval"] == "required" and ORDER.index(wanted) > ORDER.index("approve_to_run"):
        return Decision("approve_to_run", f"{runbook['id']} always needs an engineer's approval")
    return Decision(wanted, f"autonomy for {runbook['id']} at {tenant.id} is {wanted}")
