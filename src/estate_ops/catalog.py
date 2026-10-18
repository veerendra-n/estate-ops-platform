"""The runbook catalog published by ops-foundation (build/catalog.json) and its alert specs.

The platform only reads metadata. Runbooks stay in ops-foundation and run through Semaphore, so there is
one tested copy of each. Alert specs come from the catalog's "alerts" key when present, otherwise from
an ops-foundation checkout's observability/alerts folder.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CATALOG_SCHEMA = "ops-foundation/catalog/v1"
# Run parameters the platform sets itself; never taken from an alert or a model.
RESERVED = {"rb_tenant", "rb_targets", "rb_dry_run", "rb_approval_ref", "rb_run_id"}


@dataclass(frozen=True)
class Alert:
    id: str
    title: str
    severity: str
    runbook: str | None
    runbook_params: dict[str, str]


class Catalog:
    def __init__(self, data: dict[str, Any], alerts: list[Alert]):
        if data.get("schema") != CATALOG_SCHEMA:
            raise ValueError(f"catalog schema {data.get('schema')!r} is not {CATALOG_SCHEMA}")
        self.commit: str | None = data.get("commit")
        self._runbooks = {rb["id"]: rb for rb in data["runbooks"]}
        self._alerts = {a.id: a for a in alerts}

    @classmethod
    def load(cls, catalog_file: Path, alerts_dir: Path | None = None) -> Catalog:
        data = json.loads(catalog_file.read_text(encoding="utf-8"))
        raw_alerts = list(data.get("alerts", []))
        if not raw_alerts and alerts_dir and alerts_dir.is_dir():
            raw_alerts = [yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(alerts_dir.glob("*.yml"))]
        alerts = [
            Alert(
                id=a["id"],
                title=a.get("title", a["id"]),
                severity=a.get("severity", "P3"),
                runbook=a.get("runbook"),
                runbook_params=dict(a.get("runbook_params") or {}),
            )
            for a in raw_alerts
        ]
        return cls(data, alerts)

    def runbook(self, runbook_id: str) -> dict[str, Any]:
        try:
            return self._runbooks[runbook_id]
        except KeyError:
            raise KeyError(f"runbook {runbook_id!r} is not in the catalog") from None

    def runbook_ids(self) -> list[str]:
        return sorted(self._runbooks)

    def alert(self, alert_id: str) -> Alert | None:
        return self._alerts.get(alert_id)

    def runbooks_for_alert(self, alert_id: str) -> list[str]:
        """Runbooks that name this alert as a trigger, plus the one the alert spec names."""
        ids = {rb_id for rb_id, rb in self._runbooks.items() if alert_id in (rb.get("triggers") or [])}
        spec = self.alert(alert_id)
        if spec and spec.runbook in self._runbooks:
            ids.add(spec.runbook)
        return sorted(ids)

    def summary(self, runbook_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """What an agent needs to choose a runbook: no playbook paths, no secrets list."""
        out = []
        for rb_id in runbook_ids or self.runbook_ids():
            rb = self._runbooks[rb_id]
            out.append(
                {
                    "id": rb_id,
                    "title": rb["title"],
                    "summary": rb.get("summary", ""),
                    "risk": rb["risk"],
                    "approval": rb["approval"],
                    "max_targets": rb.get("max_targets", 1),
                    "target_group": (rb.get("targets") or {}).get("group"),
                    "params": [
                        {k: p[k] for k in ("name", "type", "required", "default", "choices", "description") if k in p}
                        for p in rb.get("params", [])
                    ],
                    "notes": rb.get("notes", ""),
                }
            )
        return out

    def validate_params(self, runbook_id: str, params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Check parameters against the runbook's meta.yml. Returns (clean params, errors)."""
        rb = self.runbook(runbook_id)
        specs = {p["name"]: p for p in rb.get("params", [])}
        clean: dict[str, Any] = {}
        errors: list[str] = []
        for name, value in params.items():
            if name in RESERVED:
                errors.append(f"{name} is set by the platform, not by a proposal")
                continue
            spec = specs.get(name)
            if spec is None:
                errors.append(f"{name} is not a parameter of {runbook_id}")
                continue
            if value is None or value == "":
                continue
            try:
                clean[name] = _coerce(spec, value)
            except ValueError as exc:
                errors.append(f"{name}: {exc}")
        for name, spec in specs.items():
            if spec.get("required") and name not in clean:
                errors.append(f"{name} is required by {runbook_id}")
        return clean, errors


def _coerce(spec: dict[str, Any], value: Any) -> Any:
    kind = spec.get("type", "string")
    if kind == "integer":
        if isinstance(value, bool):
            raise ValueError("expected an integer")
        return int(value)
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("true", "false"):
            return str(value).lower() == "true"
        raise ValueError("expected true or false")
    if kind == "enum":
        if value not in spec.get("choices", []):
            raise ValueError(f"expected one of {spec.get('choices')}")
        return value
    text = str(value)
    if len(text) > 256 or any(c in text for c in "\n\r\x00"):
        raise ValueError("expected a short single-line string")
    return text
