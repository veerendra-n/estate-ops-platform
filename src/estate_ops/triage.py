"""Alert intake and deterministic triage. No model is called here.

An OpenObserve alert arrives as one webhook with one row per affected target. The API checks it and puts
it on NATS (intake.py); the worker takes it from there and runs ingest(). Each row becomes a signal.
Signals with the same fingerprint (tenant, alert, target) fold into the one open case for it, so repeat
firings don't open new cases. A message delivered twice adds nothing: a signal is keyed by
(intake id, row number). The worker starts a case workflow for every case still open.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import psycopg

from . import ledger
from .catalog import Catalog

TARGET_COLUMNS = ("host_name", "host", "caller_computer_name", "device_name")


class IntakeError(ValueError):
    pass


@dataclass
class Triaged:
    case_id: str
    ref: str
    new: bool
    target: str | None
    open: bool  # still waiting for its workflow to diagnose it


def parse_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Accept rows as a list of objects, a list of JSON strings, or newline-separated JSON text."""
    rows = payload.get("rows", [])
    if isinstance(rows, str):
        rows = [r for r in rows.splitlines() if r.strip()]
    out = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, str):
            try:
                r = json.loads(r)
            except json.JSONDecodeError as exc:
                raise IntakeError(f"row is not JSON: {r[:80]!r}") from exc
        if isinstance(r, dict):
            out.append(r)
    return out


def fingerprint(tenant_id: str, alert_id: str, target: str | None) -> str:
    return hashlib.sha256(f"{tenant_id}|{alert_id}|{target or '-'}".encode()).hexdigest()[:32]


def target_of(catalog: Catalog, alert_id: str, row: dict[str, Any]) -> str | None:
    spec = catalog.alert(alert_id)
    column = spec.runbook_params.get("rb_targets") if spec else None
    for col in ([column] if column else []) + list(TARGET_COLUMNS):
        if row.get(col):
            return str(row[col])
    return None


def check(tenant_id: str, payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """The alert id and rows of a payload, or IntakeError. Run by the API before queueing, and again here."""
    alert_id = str(payload.get("alert_id") or payload.get("alert_name") or "").strip()
    if not alert_id:
        raise IntakeError("payload has no alert_id")
    rows = parse_rows(payload) or [{}]
    # A row stamped with another customer's tenant_id is refused, whatever the URL said.
    if any(row.get("tenant_id") not in (None, tenant_id) for row in rows):
        raise IntakeError("row tenant_id does not match the intake tenant")
    return alert_id, rows


def ingest(conn: psycopg.Connection, tenant_id: str, catalog: Catalog, payload: dict[str, Any], intake_id: str) -> list[Triaged]:
    alert_id, rows = check(tenant_id, payload)
    spec = catalog.alert(alert_id)
    results: list[Triaged] = []
    for row_no, row in enumerate(rows):
        seen = conn.execute(
            "SELECT c.id, c.ref, c.target, c.status FROM signals s JOIN cases c ON c.id = s.case_id "
            "WHERE s.tenant_id = %s AND s.intake_id = %s AND s.row_no = %s",
            (tenant_id, intake_id, row_no),
        ).fetchone()
        if seen:  # redelivered message
            results.append(Triaged(str(seen["id"]), seen["ref"], False, seen["target"], seen["status"] == "open"))
            continue
        target = target_of(catalog, alert_id, row)
        fp = fingerprint(tenant_id, alert_id, target)
        existing = conn.execute(
            "SELECT id, ref, status FROM cases WHERE tenant_id = %s AND fingerprint = %s "
            "AND status NOT IN ('resolved', 'escalated') FOR UPDATE",
            (tenant_id, fp),
        ).fetchone()
        new = existing is None
        if new:
            title = f"{spec.title if spec else alert_id} on {target}" if target else (spec.title if spec else alert_id)
            existing = conn.execute(
                "INSERT INTO cases (tenant_id, ref, title, severity, status, fingerprint, target) "
                "VALUES (%s, upper(%s) || '-' || lpad(nextval('case_ref_seq')::text, 5, '0'), %s, %s, 'open', %s, %s) "
                "RETURNING id, ref, status",
                (tenant_id, tenant_id, title, spec.severity if spec else "P3", fp, target),
            ).fetchone()
        else:
            conn.execute("UPDATE cases SET updated_at = now() WHERE id = %s", (existing["id"],))
        conn.execute(
            "INSERT INTO signals (tenant_id, case_id, intake_id, row_no, source, alert_id, fingerprint, target, labels) "
            "VALUES (%s, %s, %s, %s, 'openobserve', %s, %s, %s, %s)",
            (tenant_id, existing["id"], intake_id, row_no, alert_id, fp, target, json.dumps(row, default=str)),
        )
        if new:
            ledger.append(
                conn,
                tenant_id,
                existing["id"],
                "triage",
                "case_opened",
                {
                    "alert_id": alert_id,
                    "target": target,
                    "labels": row,
                    "alert_url": payload.get("url"),
                    "known_alert": spec is not None,
                },
            )
        results.append(Triaged(str(existing["id"]), existing["ref"], new, target, existing["status"] == "open"))
    return results
