"""Microsoft Teams: a card in the customer's team channel when a case needs a person or has been fixed.

Cards go to a channel through a Teams Workflows webhook (the replacement for Office 365 connectors), one
URL per customer channel, named by `teams_webhook_env` in tenants.yml. A webhook card can open links but
cannot carry buttons that call back, so "Approve" opens the case in the console, where the engineer is
signed in. Approving inside Teams needs a bot registration; that is a later step.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..tenancy import Tenant
from . import case_link

SEVERITY_COLOR = {"P1": "Attention", "P2": "Attention", "P3": "Warning", "P4": "Default"}


def card_for(entry: dict[str, Any], case: dict[str, Any], tenant: Tenant, link: str) -> dict[str, Any] | None:
    kind, b = entry["kind"], entry.get("body") or {}
    if kind == "approval_requested":
        headline, text, button = "Approval needed", f"Dry run found: {b.get('reason') or b.get('action')}", "Review and approve"
    elif kind == "escalated":
        headline, text, button = "Needs an engineer", b.get("reason") or "", "Open the case"
    elif kind == "run_result" and not b.get("dry_run") and b.get("outcome") in ("fixed", "no_action"):
        reason = ((b.get("evidence") or {}).get("decision") or {}).get("reason", "")
        headline, text, button = "Fixed by runbook", reason, "See what was done"
    else:
        return None
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": f"{headline}: {case['ref']}",
                "weight": "Bolder",
                "size": "Medium",
                "color": SEVERITY_COLOR.get(case["severity"], "Default"),
                "wrap": True,
            },
            {"type": "TextBlock", "text": case["title"], "wrap": True},
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Customer", "value": tenant.name},
                    {"title": "Target", "value": case.get("target") or "-"},
                    {"title": "Severity", "value": case["severity"]},
                ],
            },
            {"type": "TextBlock", "text": text[:600], "wrap": True, "isSubtle": True},
        ],
        "actions": [{"type": "Action.OpenUrl", "title": button, "url": link}],
    }
    return {
        "type": "message",
        "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}],
    }


class Teams:
    name = "teams"

    def __init__(self, platform, client: httpx.Client | None = None):
        self.platform = platform
        self.http = client or httpx.Client(timeout=30)

    def enabled(self, tenant: Tenant) -> bool:
        return bool(tenant.teams_webhook())

    def deliver(self, tenant: Tenant, entry: dict[str, Any], case: dict[str, Any]) -> None:
        message = card_for(entry, case, tenant, case_link(self.platform.settings.public_url, str(case["id"])))
        if message is None:
            return
        r = self.http.post(tenant.teams_webhook(), json=message)
        r.raise_for_status()
