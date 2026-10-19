"""Alert intake and deterministic triage. No model is called here.

An alert arrives as one payload with one row per affected thing (see sources.py for how vmalert, Veeam and
Microsoft 365 alerts get this shape). The API checks it and puts it on NATS; the worker runs ingest().

Each row becomes a signal and is matched to an estate graph asset by its labels (datastore, host, service...).
Then it folds into a case, in this order:
1. a repeat: an open case already holds a signal with the same fingerprint (tenant, alert, asset);
2. a symptom of the same problem: an open case touched in the last CORRELATE_MINUTES has an asset within
   CORRELATE_HOPS relations of this one (not through a hypervisor host or cluster), so a slow datastore and
   the twenty alerts it causes upstream become one case;
3. otherwise a new case, with SLA clocks from the customer's contract at the alert's priority.
A message delivered twice adds nothing: a signal is keyed by (intake id, row number).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import psycopg

from . import ledger
from .tenancy import Tenant

# Label -> graph asset kind, in the order a row's labels are tried.
ASSET_LABELS = (
    ("datastore", "datastore"),
    ("repository", "backup_repository"),
    ("backup_job", "backup_job"),
    ("hypervisor", "hypervisor_host"),
    ("host", "server"),
    ("device", "endpoint"),
    ("m365_service", "m365_service"),
    ("service", "service"),
)
SEVERITIES = ("P1", "P2", "P3", "P4")
OPEN = ("open", "diagnosing", "awaiting_approval", "running")


class IntakeError(ValueError):
    pass


@dataclass
class Triaged:
    case_id: str
    ref: str
    new: bool
    target: str | None
    open: bool  # its workflow should be running


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


def check(tenant_id: str, payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """The alert id and rows of a payload, or IntakeError. Run by the API before queueing, and again here."""
    alert_id = str(payload.get("alert_id") or "").strip()
    if not alert_id:
        raise IntakeError("payload has no alert_id")
    if len(alert_id) > 120:
        raise IntakeError("alert_id is too long")
    if payload.get("event", "firing") not in ("firing", "cleared"):
        raise IntakeError("event must be firing or cleared")
    rows = parse_rows(payload) or [{}]
    if len(rows) > 500:
        raise IntakeError("more than 500 rows in one alert")
    # A row stamped with another customer's tenant_id is refused, whatever the URL said.
    if any(row.get("tenant_id") not in (None, tenant_id) for row in rows):
        raise IntakeError("row tenant_id does not match the intake tenant")
    return alert_id, rows


def asset_of(row: dict[str, Any]) -> tuple[str | None, str | None]:
    """(graph asset id, target name) the row's labels point at."""
    for label, kind in ASSET_LABELS:
        value = row.get(label)
        if value not in (None, ""):
            return f"{kind}:{value}", str(value)
    return None, None


def fingerprint(tenant_id: str, alert_id: str, target: str | None) -> str:
    return hashlib.sha256(f"{tenant_id}|{alert_id}|{target or '-'}".encode()).hexdigest()[:32]


def _severity(payload: dict[str, Any]) -> str:
    severity = str(payload.get("severity") or "P3").upper()
    return severity if severity in SEVERITIES else "P3"


def _correlated(conn, graph, tenant_id: str, asset_id: str | None, hops: int, minutes: float):
    if graph is None or asset_id is None:
        return None
    candidates = conn.execute(
        "SELECT id, ref, status, assets FROM cases WHERE tenant_id = %s AND status = ANY(%s) "
        "AND updated_at > now() - make_interval(secs => %s) AND cardinality(assets) > 0 ORDER BY updated_at DESC LIMIT 10",
        (tenant_id, list(OPEN), minutes * 60),
    ).fetchall()
    for c in candidates:
        try:
            if asset_id in c["assets"] or graph.related(tenant_id, [asset_id], list(c["assets"]), hops):
                return c
        except Exception:  # Neo4j down: open a separate case rather than block intake
            return None
    return None


def _service_of(graph, tenant_id: str, row: dict[str, Any], asset_id: str | None) -> str | None:
    if row.get("service"):
        return str(row["service"])
    if graph is None or asset_id is None:
        return None
    try:
        services = graph.blast_radius(tenant_id, [asset_id])["services"]
    except Exception:
        return None
    return (
        services[0]
        if len(services) == 1
        else (", ".join(services[:2]) + ("…" if len(services) > 2 else "") if services else None)
    )


def ingest(
    conn: psycopg.Connection,
    tenant: Tenant,
    payload: dict[str, Any],
    intake_id: str,
    graph=None,
    hops: int = 2,
    minutes: float = 30,
) -> list[Triaged]:
    tenant_id = tenant.id
    alert_id, rows = check(tenant_id, payload)
    source = str(payload.get("source") or "webhook")[:40]
    severity = _severity(payload)
    # One triage at a time per customer, so two workers can't open two cases for one problem.
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"triage:{tenant_id}",))
    # Take the ledger's lock before any case row, in the order every other step takes them (ledger, then rows),
    # so triage folding into a case can't deadlock with that case's workflow writing to it.
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('ledger:' || %s))", (tenant_id,))
    results: list[Triaged] = []
    for row_no, row in enumerate(rows):
        seen = conn.execute(
            "SELECT c.id, c.ref, c.target, c.status FROM signals s JOIN cases c ON c.id = s.case_id "
            "WHERE s.tenant_id = %s AND s.intake_id = %s AND s.row_no = %s",
            (tenant_id, intake_id, row_no),
        ).fetchone()
        if seen:  # redelivered message
            results.append(Triaged(str(seen["id"]), seen["ref"], False, seen["target"], seen["status"] in OPEN))
            continue
        asset_id, target = asset_of(row)
        fp = fingerprint(tenant_id, alert_id, asset_id or target)
        if payload.get("event") == "cleared":
            results += _cleared(conn, tenant_id, alert_id, source, row, row_no, fp, asset_id, target, intake_id)
            continue
        case = conn.execute(
            "SELECT c.id, c.ref, c.status, c.target, c.assets FROM signals s JOIN cases c ON c.id = s.case_id "
            "WHERE s.tenant_id = %s AND s.fingerprint = %s AND c.status = ANY(%s) ORDER BY s.id DESC LIMIT 1 FOR UPDATE OF c",
            (tenant_id, fp, list(OPEN)),
        ).fetchone()
        how = "repeat"
        if case is None:
            case = _correlated(conn, graph, tenant_id, asset_id, hops, minutes)
            how = "correlated"
        new = case is None
        if new:
            title = str(payload.get("title") or alert_id).strip()[:200]
            if target and target not in title:
                title = f"{title} on {target}"
            sla = tenant.sla(severity)
            case = conn.execute(
                "INSERT INTO cases (tenant_id, ref, type, title, severity, status, source, fingerprint, target, assets, service, "
                "respond_due, resolve_due) VALUES (%s, upper(%s) || '-' || lpad(nextval('case_ref_seq')::text, 5, '0'), "
                "'incident', %s, %s, 'open', %s, %s, %s, %s, %s, now() + %s, now() + %s) "
                "RETURNING id, ref, status, target, assets",
                (
                    tenant_id,
                    tenant_id,
                    title,
                    severity,
                    source,
                    fp,
                    target,
                    [asset_id] if asset_id else [],
                    _service_of(graph, tenant_id, row, asset_id),
                    timedelta(minutes=sla["respond"]),
                    timedelta(minutes=sla["resolve"]),
                ),
            ).fetchone()
        else:
            _fold(conn, tenant, case, asset_id, severity)
        conn.execute(
            "INSERT INTO signals (tenant_id, case_id, intake_id, row_no, source, alert_id, fingerprint, target, asset_id, "
            "labels) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (tenant_id, case["id"], intake_id, row_no, source, alert_id, fp, target, asset_id, json.dumps(row, default=str)),
        )
        body = {
            "alert_id": alert_id,
            "source": source,
            "target": target,
            "asset_id": asset_id,
            "severity": severity,
            "labels": row,
        }
        if new:
            ledger.append(conn, tenant_id, case["id"], "triage", "case_opened", body | {"alert_url": payload.get("url")})
        elif how == "correlated" or asset_id not in (case["assets"] or []):
            ledger.append(conn, tenant_id, case["id"], "triage", "alert_folded", body | {"why": how})
        survivor = _merge(conn, tenant, graph, case, asset_id, hops, minutes)
        if survivor["id"] != case["id"]:
            new = False
        results.append(Triaged(str(survivor["id"]), survivor["ref"], new, target, True))
    return results


def _merge(conn, tenant: Tenant, graph, case: dict[str, Any], asset_id: str | None, hops: int, minutes: float):
    """Symptoms can arrive before their cause: two slow services open two cases, then the datastore under both
    arrives and folds into one of them. Cases this alert links to that are still settling (nothing investigated
    yet) are merged into the oldest; the others close as merged and point at it. Returns the surviving case."""
    if graph is None or asset_id is None:
        return case
    cols = "id, ref, status, assets, severity, opened_at"
    others = conn.execute(
        f"SELECT {cols} FROM cases WHERE tenant_id = %s AND status = 'open' "
        "AND id <> %s AND updated_at > now() - make_interval(secs => %s) AND cardinality(assets) > 0 "
        "ORDER BY opened_at LIMIT 10",
        (tenant.id, case["id"], minutes * 60),
    ).fetchall()
    try:
        linked = [c for c in others if asset_id in c["assets"] or graph.related(tenant.id, [asset_id], list(c["assets"]), hops)]
    except Exception:  # Neo4j down: leave the cases apart
        return case
    if not linked:
        return case
    current = conn.execute(f"SELECT {cols} FROM cases WHERE id = %s", (case["id"],)).fetchone()
    group = [current, *linked]
    survivor = current if current["status"] != "open" else min(group, key=lambda c: c["opened_at"])
    for c in group:
        if c["id"] == survivor["id"]:
            continue
        conn.execute("UPDATE signals SET case_id = %s WHERE case_id = %s", (survivor["id"], c["id"]))
        for a in c["assets"]:
            _fold(conn, tenant, survivor, a, c["severity"])
        conn.execute(
            "UPDATE cases SET status = 'merged', parent_id = %s, resolved_at = now(), updated_at = now() WHERE id = %s",
            (survivor["id"], c["id"]),
        )
        ledger.append(conn, tenant.id, c["id"], "triage", "merged_into", {"into": survivor["ref"], "via": asset_id})
        ledger.append(conn, tenant.id, survivor["id"], "triage", "cases_merged", {"merged": c["ref"], "via": asset_id})
    return survivor


def _fold(conn, tenant: Tenant, case: dict[str, Any], asset_id: str | None, severity: str) -> None:
    """Add the alert's asset to the case and raise its priority (and SLA clocks) if this alert is worse."""
    conn.execute(
        "UPDATE cases SET updated_at = now(), "
        "assets = CASE WHEN %(a)s::text IS NULL OR %(a)s = ANY(assets) THEN assets ELSE array_append(assets, %(a)s) END "
        "WHERE id = %(id)s",
        {"a": asset_id, "id": case["id"]},
    )
    sla = tenant.sla(severity)
    conn.execute(
        "UPDATE cases SET severity = %s, respond_due = least(respond_due, opened_at + %s), "
        "resolve_due = least(resolve_due, opened_at + %s) WHERE id = %s AND severity > %s",
        (severity, timedelta(minutes=sla["respond"]), timedelta(minutes=sla["resolve"]), case["id"], severity),
    )


def _cleared(conn, tenant_id, alert_id, source, row, row_no, fp, asset_id, target, intake_id) -> list[Triaged]:
    """The source says the problem is gone. Noted on the open case holding that alert; the case itself carries
    on, because a cleared alert is not proof of a fix (a skill may be half way through)."""
    case = conn.execute(
        "SELECT c.id, c.ref FROM signals s JOIN cases c ON c.id = s.case_id "
        "WHERE s.tenant_id = %s AND s.fingerprint = %s AND c.status = ANY(%s) ORDER BY s.id DESC LIMIT 1",
        (tenant_id, fp, list(OPEN)),
    ).fetchone()
    if case is None:
        return []
    conn.execute(
        "INSERT INTO signals (tenant_id, case_id, intake_id, row_no, source, alert_id, fingerprint, target, asset_id, labels) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (tenant_id, case["id"], intake_id, row_no, source, alert_id, fp, target, asset_id, json.dumps(row, default=str)),
    )
    ledger.append(conn, tenant_id, case["id"], f"source:{source}", "alert_cleared", {"alert_id": alert_id, "target": target})
    return [Triaged(str(case["id"]), case["ref"], False, target, False)]
