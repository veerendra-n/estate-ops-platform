"""Customers (tenants) and their settings, loaded from config/tenants.yml."""

from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import yaml

AUTONOMY_MODES = ("recommend", "approve_to_run", "run_and_notify")


# Default service levels (minutes) when a customer's contract doesn't set its own.
DEFAULT_SLA = {
    "P1": {"respond": 15, "resolve": 240},
    "P2": {"respond": 30, "resolve": 480},
    "P3": {"respond": 240, "resolve": 2880},
    "P4": {"respond": 480, "resolve": 7200},
}


@dataclass(frozen=True)
class Tenant:
    id: str
    name: str
    intake_token_env: str
    industry: str = ""
    diagnosis: str = "llm"
    ai_budget_usd_month: float = 40.0
    autonomy: dict[str, str] = field(default_factory=lambda: {"default": "approve_to_run"})
    # contract: {sla: {P1: {respond, resolve}, ...}, change_freeze: [{from, to, reason}], maintenance: "Sun 01:00-05:00"}
    contract: dict[str, Any] = field(default_factory=dict)
    # connectors: {vcenter|veeam|microsoft|fleet|edge: {url, ..., *_env}}; see connectors/__init__.py.
    connectors: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Teams channel for notices and approval links (a Workflows webhook URL in this variable).
    teams_webhook_env: str | None = None
    # Declared facts: business services, owners and criticality (config/estate/<tenant>.yml).
    declared_file: str | None = None

    def sla(self, priority: str) -> dict[str, int]:
        return (self.contract.get("sla") or {}).get(priority) or DEFAULT_SLA.get(priority, DEFAULT_SLA["P3"])

    def autonomy_for(self, skill_id: str) -> str:
        mode = self.autonomy.get(skill_id, self.autonomy.get("default", "approve_to_run"))
        if mode not in AUTONOMY_MODES:
            raise ValueError(f"{self.id}: unknown autonomy mode {mode!r} for {skill_id}")
        return mode

    def intake_token(self) -> str | None:
        return os.environ.get(self.intake_token_env)

    def teams_webhook(self) -> str | None:
        return os.environ.get(self.teams_webhook_env) if self.teams_webhook_env else None

    def intake_token_ok(self, presented: str | None) -> bool:
        expected = os.environ.get(self.intake_token_env)
        if not expected or not presented:
            return False
        return hmac.compare_digest(expected.encode(), presented.encode())


class Tenants:
    def __init__(self, tenants: list[Tenant]):
        self._by_id = {t.id: t for t in tenants}

    @classmethod
    def load(cls, path: Path) -> Tenants:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        tenants = [Tenant(**t) for t in raw.get("tenants", [])]
        for t in tenants:
            if not t.id.isascii() or not t.id.replace("-", "").isalnum() or t.id != t.id.lower():
                raise ValueError(f"tenant id {t.id!r} must be short, lowercase letters, digits or dashes")
            if t.diagnosis not in ("llm", "deterministic"):
                raise ValueError(f"{t.id}: diagnosis must be llm or deterministic")
            for rb in t.autonomy:
                t.autonomy_for(rb)
        return cls(tenants)

    def get(self, tenant_id: str) -> Tenant:
        try:
            return self._by_id[tenant_id]
        except KeyError:
            raise KeyError(f"unknown tenant {tenant_id!r}") from None

    def ids(self) -> list[str]:
        return list(self._by_id)

    def __iter__(self):
        return iter(self._by_id.values())

    def sync(self, database_url: str) -> None:
        """Write the tenant list to the database (as owner: estate_app can only read it)."""
        with psycopg.connect(database_url) as conn:
            for t in self:
                conn.execute(
                    "INSERT INTO tenants (id, name, config) VALUES (%s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, config = EXCLUDED.config",
                    (t.id, t.name, json.dumps({"autonomy": t.autonomy, "diagnosis": t.diagnosis, "industry": t.industry})),
                )
