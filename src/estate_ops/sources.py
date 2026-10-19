"""Where cases and the estate graph get their facts, per customer, and the health of each feed.

- Metric and log alerts: vmalert evaluates the rules in alerts/ on VictoriaMetrics and VictoriaLogs and posts
  firing and resolved alerts, Alertmanager style, to /api/v1/intake/alertmanager/api/v2/alerts. Every rule
  carries tenant_id; alertmanager() turns each alert into the platform's alert shape (see triage.py).
- Other webhooks post to /api/v1/intake/<source>/<tenant>; WEBHOOKS lists the shapes accepted.
- Polled alerts (Veeam job sessions, Microsoft 365 service health) are read every POLL_INTERVAL_MINUTES. The
  intake id is stable per event, so polling again adds nothing.
- Inventory (discovery.py) writes the estate graph every DISCOVERY_INTERVAL_MINUTES.

Everything here reads; nothing writes to a customer's systems. Each run's outcome goes to integration_status,
which the cockpit's Integrations page shows.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from . import discovery, triage
from .connectors import Connectors, NotConfigured
from .tenancy import Tenant

log = logging.getLogger("estate_ops.sources")


def _id(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:40]


def _ended(ts: str | None) -> bool:
    """Alertmanager marks a resolved alert with an endsAt in the past (an open one has none, or one ahead)."""
    if not ts or ts.startswith("0001-"):
        return False
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")) <= datetime.now(UTC)
    except ValueError:
        return False


def alertmanager(alerts: list[dict[str, Any]]) -> list[tuple[str, str, dict[str, Any]]]:
    """vmalert's Alertmanager v2 post -> [(tenant_id, intake_id, payload)]. An alert without tenant_id is
    refused: a rule that forgets it would otherwise land on nobody's case list."""
    out = []
    for a in alerts:
        labels = {k: str(v) for k, v in (a.get("labels") or {}).items()}
        tenant = labels.pop("tenant_id", "")
        name = labels.pop("alertname", "")
        if not tenant or not name:
            raise triage.IntakeError("every alert needs tenant_id and alertname labels")
        annotations = a.get("annotations") or {}
        event = "cleared" if _ended(a.get("endsAt")) else "firing"
        payload = {
            "source": "metrics",
            "alert_id": name,
            "title": annotations.get("title") or annotations.get("summary") or name,
            "severity": labels.pop("severity", "P3").upper(),
            "event": event,
            "url": a.get("generatorURL"),
            "rows": [labels | {k: v for k, v in annotations.items() if k in ("value", "description")}],
        }
        identity = json.dumps(sorted(labels.items()))
        out.append((tenant, _id(tenant, name, identity, a.get("startsAt"), event), payload))
    return out


def _generic(tenant: Tenant, body: dict[str, Any]) -> list[dict[str, Any]]:
    return [body | {"source": str(body.get("source") or "webhook")[:40]}]


WEBHOOKS: dict[str, Callable[[Tenant, dict[str, Any]], list[dict[str, Any]]]] = {"generic": _generic}


# ---- polled alert sources -----------------------------------------------------------------------------------


def veeam_alerts(conns: Connectors) -> list[tuple[str, dict[str, Any]]]:
    """The latest finished run of each job: failed fires veeam-job-failed, success clears it."""
    client = conns.veeam()
    latest: dict[str, dict[str, Any]] = {}
    for s in client.sessions():
        if s.get("state") == "Working" or s.get("sessionType", "BackupJob") != "BackupJob":
            continue
        if s["name"] not in latest or (s.get("creationTime") or "") > (latest[s["name"]].get("creationTime") or ""):
            latest[s["name"]] = s
    out = []
    for job, s in latest.items():
        result = ((s.get("result") or {}).get("result") or "").lower()
        if result not in ("failed", "success"):
            continue
        event = "firing" if result == "failed" else "cleared"
        out.append(
            (
                _id(conns.tenant.id, "veeam", s["id"], event),
                {
                    "source": "veeam",
                    "alert_id": "veeam-job-failed",
                    "title": f"Backup job {job} failed",
                    "severity": "P2",
                    "event": event,
                    "rows": [{"backup_job": job, "message": (s.get("result") or {}).get("message", ""), "session": s["id"]}],
                },
            )
        )
    return out


def m365_alerts(conns: Connectors) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for issue in conns.microsoft().service_issues():
        event = "cleared" if issue.get("isResolved") else "firing"
        out.append(
            (
                _id(conns.tenant.id, "m365", issue["id"], event),
                {
                    "source": "microsoft",
                    "alert_id": "m365-service-degraded",
                    "title": f"{issue.get('service')}: {issue.get('title', 'service degraded')}",
                    "severity": "P2" if issue.get("classification") == "incident" else "P3",
                    "event": event,
                    "rows": [
                        {"m365_service": issue.get("service"), "issue": issue["id"], "impact": issue.get("impactDescription", "")}
                    ],
                },
            )
        )
    return out


@dataclass(frozen=True)
class Poller:
    name: str
    connector: str
    run: Callable[[Connectors], list[tuple[str, dict[str, Any]]]]


POLLERS = [Poller("veeam-sessions", "veeam", veeam_alerts), Poller("m365-health", "microsoft", m365_alerts)]


# ---- status ---------------------------------------------------------------------------------------------------


def configured(platform) -> dict[str, list[tuple[str, str]]]:
    """Per tenant, the (name, kind) of every feed switched on for it."""
    from .worker import configured_integrations

    events = configured_integrations(platform)
    out: dict[str, list[tuple[str, str]]] = {}
    for t in platform.tenants:
        rows = [("metrics-alerts", "alerts")]
        rows += [(p.name, "alerts") for p in POLLERS if p.connector in (t.connectors or {})]
        rows += [
            (name, "inventory")
            for name, needs, _ in discovery.collectors(platform.settings)
            if needs is None or needs in (t.connectors or {})
        ]
        rows += [(i.name, "events") for i in events if i.enabled(t)]
        out[t.id] = rows
    return out


def record(platform, tenant_id: str, name: str, kind: str, ok: bool, detail: dict[str, Any], error: str | None = None) -> None:
    """Upsert one feed's last run. detail keys ending in _total add up across runs."""
    totals = {k: v for k, v in detail.items() if k.endswith("_total")}
    latest = {k: v for k, v in detail.items() if not k.endswith("_total")}
    with platform.db.scope(tenant_id) as conn:
        row = conn.execute(
            "SELECT detail FROM integration_status WHERE tenant_id = %s AND name = %s FOR UPDATE", (tenant_id, name)
        ).fetchone()
        merged = dict(row["detail"]) if row else {}
        merged.update(latest)
        for k, v in totals.items():
            merged[k] = int(merged.get(k, 0)) + int(v)
        conn.execute(
            "INSERT INTO integration_status (tenant_id, name, kind, ok, last_run_at, last_ok_at, last_error, detail) "
            "VALUES (%(t)s, %(n)s, %(k)s, %(ok)s, now(), CASE WHEN %(ok)s THEN now() END, %(err)s, %(d)s) "
            "ON CONFLICT (tenant_id, name) DO UPDATE SET kind = EXCLUDED.kind, ok = EXCLUDED.ok, last_run_at = now(), "
            "last_ok_at = CASE WHEN EXCLUDED.ok THEN now() ELSE integration_status.last_ok_at END, "
            "last_error = CASE WHEN EXCLUDED.ok THEN integration_status.last_error ELSE EXCLUDED.last_error END, "
            "detail = EXCLUDED.detail",
            {
                "t": tenant_id,
                "n": name,
                "k": kind,
                "ok": ok,
                "err": (error or "")[:500] or None,
                "d": json.dumps(merged, default=str),
            },
        )


def _error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} from {exc.request.url.host}{exc.request.url.path}"
    return f"{type(exc).__name__}: {exc}"[:500]


# ---- runs -----------------------------------------------------------------------------------------------------


def discover(platform, tenant: Tenant, name: str, collect) -> dict[str, Any]:
    """One inventory read for one tenant: the source's current view replaces its previous one."""
    try:
        conns = platform.connectors(tenant)
        assets, relations = collect(tenant, conns)
        before = platform.graph.snapshot(tenant.id, name)
        changes = discovery.diff(name, before, assets)
        platform.graph.drop_relations(tenant.id, name)
        platform.graph.upsert(tenant.id, name, assets, relations)
        removed = platform.graph.prune(tenant.id, name, [a["id"] for a in assets])
    except Exception as exc:  # one customer's outage must not stop the others
        log.warning("%s discovery for %s failed: %s", name, tenant.id, _error(exc))
        record(platform, tenant.id, name, "inventory", False, {}, _error(exc))
        return {"error": _error(exc)}
    if changes:
        with platform.db.scope(tenant.id) as conn:
            for c in changes:
                conn.execute(
                    "INSERT INTO graph_changes (tenant_id, source, asset_id, asset_name, change, before, after) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (tenant.id, c["source"], c["asset_id"], c["asset_name"], c["change"], _json(c["before"]), _json(c["after"])),
                )
    result = {"assets": len(assets), "relations": len(relations), "removed": removed, "changes": len(changes)}
    record(platform, tenant.id, name, "inventory", True, result)
    return result


def discover_all(platform, tenants: list[Tenant] | None = None) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for tenant in tenants or list(platform.tenants):
        for name, needs, collect in discovery.collectors(platform.settings):
            if needs is None or needs in (tenant.connectors or {}):
                results.setdefault(tenant.id, {})[name] = discover(platform, tenant, name, collect)
    return results


def poll(platform, tenant: Tenant, poller: Poller) -> list[tuple[str, dict[str, Any]]]:
    """One alert poll: the alerts to queue, already checked like a webhook. Failures are recorded, not raised."""
    try:
        alerts = poller.run(platform.connectors(tenant))
        for _, payload in alerts:
            triage.check(tenant.id, payload)
    except NotConfigured:
        return []
    except Exception as exc:
        log.warning("%s poll for %s failed: %s", poller.name, tenant.id, _error(exc))
        record(platform, tenant.id, poller.name, "alerts", False, {}, _error(exc))
        return []
    firing = sum(1 for _, p in alerts if p.get("event") != "cleared")
    record(platform, tenant.id, poller.name, "alerts", True, {"firing": firing, "cleared": len(alerts) - firing})
    return alerts


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str)
