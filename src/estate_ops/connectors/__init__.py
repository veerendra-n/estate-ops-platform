"""Connectors: typed clients for a customer's systems, bound to one tenant.

Sensors (discovery, alert polling, agents' read-only tools) and actuators (skills) reach a customer's
estate only through these. Each client comes from the tenant's `connectors:` block in config/tenants.yml,
which names URLs and the environment variables that hold credentials; no secret is in the repo. A client
for one tenant is never handed to code working for another.

    vcenter   VMware vCenter (inventory, VM placement, storage vMotion, disk resize)
    veeam     Veeam Backup & Replication (jobs, sessions, repositories, start a job, remove orphaned backups)
    microsoft Microsoft Graph (Intune devices and remediations, Entra users, Microsoft 365 service health)
    fleet     Fleet / osquery (live device queries)
    edge      the customer's edge node: typed operations on Windows and Linux servers and network devices
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..tenancy import Tenant
from .telemetry import Logs, Metrics


class NotConfigured(RuntimeError):
    pass


@dataclass
class Connectors:
    tenant: Tenant
    settings: Any
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    http: httpx.Client | None = None  # tests pass an in-process client

    def _cfg(self, name: str) -> dict[str, Any]:
        cfg = (self.tenant.connectors or {}).get(name)
        if not cfg:
            raise NotConfigured(f"{name} is not set up for {self.tenant.name}")
        return cfg

    def _secret(self, cfg: dict[str, Any], key: str) -> str:
        name = cfg.get(key) or ""
        value = self.env.get(name)
        if not value:
            raise NotConfigured(f"{name or key} is not set for {self.tenant.id}")
        return value

    def _client(self, cfg: dict[str, Any]) -> httpx.Client:
        return self.http or httpx.Client(timeout=60, verify=cfg.get("verify_tls", True))

    def has(self, name: str) -> bool:
        return bool((self.tenant.connectors or {}).get(name))

    def vcenter(self):
        from .vcenter import VCenterClient

        cfg = self._cfg("vcenter")
        return VCenterClient(cfg["url"], self._secret(cfg, "username_env"), self._secret(cfg, "password_env"), self._client(cfg))

    def veeam(self):
        from .veeam import VeeamClient

        cfg = self._cfg("veeam")
        return VeeamClient(
            cfg["url"],
            self._secret(cfg, "username_env"),
            self._secret(cfg, "password_env"),
            cfg.get("api_version", "1.2-rev0"),
            self._client(cfg),
        )

    def microsoft(self):
        from .microsoft import GraphClient

        cfg = self._cfg("microsoft")
        return GraphClient(
            cfg["entra_tenant_id"],
            str(cfg["client_id"]),
            self._secret(cfg, "client_secret_env"),
            cfg.get("graph_url", "https://graph.microsoft.com"),
            cfg.get("login_url", "https://login.microsoftonline.com"),
            self._client(cfg),
        )

    def fleet(self):
        from .fleet import FleetClient

        cfg = self._cfg("fleet")
        return FleetClient(cfg["url"], self._secret(cfg, "token_env"), cfg.get("team_id"), self._client(cfg))

    def edge(self):
        from .edge import EdgeClient

        cfg = self._cfg("edge")
        return EdgeClient(cfg["url"], self._secret(cfg, "token_env"), self._client(cfg))

    def metrics(self) -> Metrics:
        return Metrics(self.settings.metrics_url, self.tenant.id, self.http)

    def logs(self) -> Logs:
        return Logs(self.settings.logs_url, self.tenant.id, self.http)
