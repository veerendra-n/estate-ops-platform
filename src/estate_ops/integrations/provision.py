"""Provisioning from the ops-foundation catalog, so nobody clicks runbooks or alerts in by hand.

- Semaphore: one task template per runbook in the customer's project, named exactly as the runbook id
  and pointing at the runbook's playbook in the ops-foundation repository.
- OpenObserve: in the customer's organisation, a webhook template and destination that post to this
  platform's intake with the customer's token, and one scheduled alert per alert spec.

Both create what is missing and report what already exists; they never delete or overwrite, so a change
made by hand in the lab survives until someone removes it. Run: python -m estate_ops provision.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from ..catalog import Alert, Catalog
from ..semaphore import Semaphore
from ..tenancy import Tenant
from ..triage import TARGET_COLUMNS

TEMPLATE_NAME = "estate-ops-intake"
# The body the intake parses (docs/integrations.md). {rows} sits in a JSON value position and each
# alert's row_template is JSON, so OpenObserve renders the rows as a JSON array of objects.
TEMPLATE_BODY = (
    '{"alert_id": "{alert_name}", "org": "{org_name}", "stream": "{stream_name}", '
    '"start": "{alert_start_time}", "end": "{alert_end_time}", "url": "{alert_url}", "rows": {rows}}'
)


def semaphore_templates(semaphore: Semaphore, catalog: Catalog, tenant: Tenant) -> dict[str, list[str]]:
    cfg = tenant.semaphore or {}
    missing_cfg = [k for k in ("inventory_id", "repository_id", "environment_id") if not cfg.get(k)]
    if missing_cfg:
        raise ValueError(f"{tenant.id}: tenants.yml semaphore needs {', '.join(missing_cfg)}")
    existing = {t["name"] for t in semaphore.templates(tenant.semaphore_project_id)}
    created, kept = [], []
    for rb_id in sorted(catalog.runbook_ids()):
        if rb_id in existing:
            kept.append(rb_id)
            continue
        rb = catalog.runbook(rb_id)
        semaphore.create_template(
            tenant.semaphore_project_id,
            {
                "name": rb_id,
                "app": "ansible",
                "playbook": rb["playbook"],
                "inventory_id": int(cfg["inventory_id"]),
                "repository_id": int(cfg["repository_id"]),
                "environment_id": int(cfg["environment_id"]),
                "description": f"{rb['title']} (ops-foundation {rb['version']}; created by estate-ops-platform)",
                "arguments": "[]",
                "allow_override_args_in_task": False,
            },
        )
        created.append(rb_id)
    return {"created": created, "existing": kept}


def _minutes(text: str) -> int:
    m = re.fullmatch(r"(\d+)\s*([mhd]?)", text.strip())
    if not m:
        raise ValueError(f"cannot read duration {text!r}")
    return int(m.group(1)) * {"": 1, "m": 1, "h": 60, "d": 1440}[m.group(2)]


def _columns(alert: Alert) -> list[str]:
    """Columns the intake needs from each row: tenant, the target, and every mapped runbook parameter."""
    cols = ["tenant_id", *alert.runbook_params.values()]
    sql = (alert.query or "").lower()
    cols += [c for c in TARGET_COLUMNS if c in sql]
    return list(dict.fromkeys(cols))


def alert_body(alert: Alert, destination: str) -> dict[str, Any]:
    row = "{" + ", ".join(f'"{c}": "{{{c}}}"' for c in _columns(alert)) + "}"
    return {
        "name": alert.id,
        "description": f"{alert.title} ({alert.severity}; ops-foundation alert spec, created by estate-ops-platform)",
        "stream_type": alert.stream_type,
        "stream_name": alert.stream,
        "is_real_time": False,
        "enabled": True,
        "query_condition": {"type": "sql", "sql": alert.query},
        "trigger_condition": {
            "period": _minutes(alert.period),
            "operator": ">=",
            "threshold": 1,
            "frequency": _minutes(alert.frequency),
            "frequency_type": "minutes",
            "silence": max(_minutes(alert.frequency), 10),
        },
        "destinations": [destination],
        "row_template": row,
    }


class OpenObserveAdmin:
    def __init__(self, base_url: str, user: str | None, password: str | None, client: httpx.Client | None = None):
        self.http = client or httpx.Client(base_url=base_url, timeout=30.0)
        if user and password:
            self.http.auth = httpx.BasicAuth(user, password)

    def _names(self, path: str, key: str = "name") -> set[str]:
        r = self.http.get(path)
        r.raise_for_status()
        body = r.json()
        items = body.get("list", body.get("alerts", [])) if isinstance(body, dict) else body
        return {i.get(key) for i in items if isinstance(i, dict)}

    def _post(self, path: str, body: dict[str, Any], **params: Any) -> None:
        r = self.http.post(path, json=body, params=params or None)
        r.raise_for_status()

    def provision(self, catalog: Catalog, tenant: Tenant, public_url: str) -> dict[str, list[str]]:
        org, token = tenant.openobserve_org, tenant.intake_token()
        if not token:
            raise ValueError(f"{tenant.id}: {tenant.intake_token_env} is not set, so OpenObserve could not authenticate")
        created, kept = [], []
        if TEMPLATE_NAME in self._names(f"/api/{org}/alerts/templates"):
            kept.append(f"template {TEMPLATE_NAME}")
        else:
            self._post(
                f"/api/{org}/alerts/templates", {"name": TEMPLATE_NAME, "type": "http", "title": "", "body": TEMPLATE_BODY}
            )
            created.append(f"template {TEMPLATE_NAME}")
        destination = f"estate-ops-{tenant.id}"
        if destination in self._names(f"/api/{org}/alerts/destinations"):
            kept.append(f"destination {destination}")
        else:
            self._post(
                f"/api/{org}/alerts/destinations",
                {
                    "name": destination,
                    "url": f"{public_url.rstrip('/')}/api/v1/intake/openobserve/{tenant.id}",
                    "method": "post",
                    "type": "http",
                    "skip_tls_verify": False,
                    "headers": {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    "template": TEMPLATE_NAME,
                },
            )
            created.append(f"destination {destination}")
        existing = self._names(f"/api/v2/{org}/alerts")
        for alert in catalog.alerts():
            if not alert.query:
                continue
            if alert.id in existing:
                kept.append(f"alert {alert.id}")
                continue
            self._post(f"/api/v2/{org}/alerts", alert_body(alert, destination), folder="default")
            created.append(f"alert {alert.id}")
        return {"created": created, "existing": kept}
