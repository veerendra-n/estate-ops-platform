"""HaloPSA: one ticket per case, a private note per step, closed when the case resolves.

HaloPSA stays the service record (SLA clocks, billing, the customer's view through Thread, which syncs
from it). The platform opens the ticket when a case opens, so even a case that resolves itself in
minutes is on the record, and writes each finding, proposal, approval and run result as a note.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from ..tenancy import Tenant
from . import case_link

OUTCOME_TEXT = {
    "fixed": "Fixed and verified",
    "pending_device": "Fix queued; waiting for the device to report back",
    "dry_run": "Dry run only; nothing was changed",
    "no_action": "Checked; nothing needed",
    "escalated": "Not safe to fix automatically; needs an engineer",
    "failed": "Runbook failed",
    "timed_out": "Runbook did not finish in time",
}


class HaloClient:
    def __init__(
        self,
        url: str,
        client_id: str,
        client_secret: str,
        tenant: str | None = None,
        client: httpx.Client | None = None,
    ):
        self.url = url.rstrip("/")
        self._creds = (client_id, client_secret)
        self._tenant = tenant
        self.http = client or httpx.Client(timeout=30)
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()

    def _auth(self) -> dict[str, str]:
        with self._lock:
            if not self._token or self._token[1] < time.time() + 60:
                r = self.http.post(
                    f"{self.url}/auth/token",
                    params={"tenant": self._tenant} if self._tenant else None,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._creds[0],
                        "client_secret": self._creds[1],
                        "scope": "all",
                    },
                )
                r.raise_for_status()
                body = r.json()
                self._token = (body["access_token"], time.time() + float(body.get("expires_in", 3600)))
            return {"Authorization": f"Bearer {self._token[0]}"}

    def _post(self, path: str, items: list[dict[str, Any]]) -> Any:
        # Halo's write endpoints take a list and return the saved record (or a list of them).
        r = self.http.post(f"{self.url}/api/{path}", json=items, headers=self._auth())
        r.raise_for_status()
        return r.json()

    def create_ticket(self, fields: dict[str, Any]) -> int:
        saved = self._post("Tickets", [fields])
        record = saved[0] if isinstance(saved, list) else saved
        return int(record["id"])

    def add_note(self, ticket_id: int, note: str, *, hidden: bool = True) -> None:
        self._post("Actions", [{"ticket_id": ticket_id, "note": note, "outcome": "Automation", "hiddenfromuser": hidden}])

    def set_status(self, ticket_id: int, status_id: int) -> None:
        self._post("Tickets", [{"id": ticket_id, "status_id": status_id}])


def note_for(entry: dict[str, Any], link: str) -> str | None:
    """Plain text an engineer reads in a few seconds, or None for entries that need no note."""
    kind, b = entry["kind"], entry.get("body") or {}
    if kind == "case_opened":
        return f"Opened by the platform from alert {b.get('alert_id')} on {b.get('target') or 'no target'}.\nCase: {link}"
    if kind == "diagnosis":
        lines = [
            f"Diagnosis ({b.get('method')}, confidence {b.get('confidence')}): {b.get('summary')}",
            f"Likely cause: {b.get('likely_cause')}",
        ]
        lines += [f"- {e.get('source')}: {e.get('finding')}" for e in b.get("evidence") or []]
        if b.get("engineer_should_check"):
            lines.append(f"Check: {b['engineer_should_check']}")
        return "\n".join(lines)
    if kind == "proposal":
        params = ", ".join(f"{k}={v}" for k, v in (b.get("params") or {}).items()) or "none"
        return (
            f"Proposed runbook {b.get('runbook_id')} v{b.get('runbook_version')} on {b.get('targets')} "
            f"({b.get('risk')} risk), parameters {params}.\nPolicy: {b.get('policy')}"
        )
    if kind == "run_result":
        ev = b.get("evidence") or {}
        decision = ev.get("decision") or {}
        lines = [
            f"{'Dry run' if b.get('dry_run') else 'Live run'}: {OUTCOME_TEXT.get(b.get('outcome'), b.get('outcome'))}",
            f"Why: {decision.get('reason', 'n/a')}",
        ]
        for act in ev.get("actions") or []:
            detail = ", ".join(f"{k}={v}" for k, v in act.items() if k not in ("step", "action"))
            lines.append(f"Did: {act.get('step') or act.get('action')}" + (f" ({detail})" if detail else ""))
        if ev.get("error"):
            lines.append(f"Error: {ev['error']}")
        return "\n".join(lines)
    if kind == "approval_requested":
        return f"Waiting for an engineer to approve: {b.get('action')}. {b.get('reason', '')}\nApprove or reject: {link}"
    if kind in ("approved", "rejected"):
        who = b.get("by") or entry.get("actor", "").removeprefix("engineer:")
        return f"{kind.capitalize()} by {who}." + (f" {b['note']}" if b.get("note") else "")
    if kind == "escalated":
        return f"Escalated to an engineer: {b.get('reason')}" + (
            f"\nCheck: {b['engineer_should_check']}" if b.get("engineer_should_check") else ""
        )
    if kind == "resolved":
        return f"Resolved: {b.get('reason')}"
    return None  # run_started and anything new: the next result note covers it


def closes_case(entry: dict[str, Any]) -> bool:
    b = entry.get("body") or {}
    if entry["kind"] == "resolved":
        return True
    return entry["kind"] == "run_result" and not b.get("dry_run") and b.get("outcome") in ("fixed", "no_action")


class HaloPSA:
    name = "halopsa"

    def __init__(self, platform, client: HaloClient):
        self.platform, self.client = platform, client

    def enabled(self, tenant: Tenant) -> bool:
        return bool(tenant.halopsa and tenant.halopsa.get("client_id"))

    def _ticket(self, tenant: Tenant, case: dict[str, Any]) -> int:
        with self.platform.db.scope(tenant.id) as conn:
            row = conn.execute(
                "SELECT ref FROM external_refs WHERE tenant_id = %s AND case_id = %s AND system = 'halopsa'",
                (tenant.id, case["id"]),
            ).fetchone()
        if row:
            return int(row["ref"])
        s, cfg = self.platform.settings, tenant.halopsa or {}
        fields = {
            "summary": f"[{case['ref']}] {case['title']}"[:250],
            "details": f"{case.get('summary') or case['title']}\n\nCase in the Estate Ops console: "
            f"{case_link(s.public_url, str(case['id']))}",
            "client_id": int(cfg["client_id"]),
            "tickettype_id": int(cfg.get("ticket_type_id", s.halo_ticket_type_id)),
            "priority_id": s.halo_priorities.get(case["severity"], 3),
        }
        if cfg.get("site_id"):
            fields["site_id"] = int(cfg["site_id"])
        if cfg.get("team"):
            fields["team"] = cfg["team"]
        ticket_id = self.client.create_ticket(fields)
        with self.platform.db.scope(tenant.id) as conn:
            conn.execute(
                "INSERT INTO external_refs (tenant_id, case_id, system, ref) VALUES (%s, %s, 'halopsa', %s) "
                "ON CONFLICT DO NOTHING",
                (tenant.id, case["id"], str(ticket_id)),
            )
        return ticket_id

    def deliver(self, tenant: Tenant, entry: dict[str, Any], case: dict[str, Any]) -> None:
        note = note_for(entry, case_link(self.platform.settings.public_url, str(case["id"])))
        if note is None and not closes_case(entry):
            return
        ticket_id = self._ticket(tenant, case)
        if note:
            self.client.add_note(ticket_id, note)
        if closes_case(entry):
            self.client.set_status(
                ticket_id, int((tenant.halopsa or {}).get("closed_status_id", self.platform.settings.halo_closed_status_id))
            )
