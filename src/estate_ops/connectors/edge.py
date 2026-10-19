"""The edge node at a customer site: typed operations on servers and network devices.

The edge node (one small VM per site, outbound-only) runs the OpenTelemetry Collector and the automation
runner. The runner exposes a fixed set of operations, each with its own argument schema and its own
PowerShell, SSH or NETCONF implementation on the edge; there is no "run this command" operation. Read
operations serve agents and discovery; write operations serve skills, after policy.
"""

from __future__ import annotations

from typing import Any

import httpx

READ_OPS = {
    "service_status",  # {host, service}
    "disk_usage",  # {host}
    "certificates",  # {host}  machine certificates with expiry
    "network_devices",  # {}  switches, routers and firewalls with model and software version
    "firewall_path",  # {source, destination, port}  does a rule allow this flow?
    "account_status",  # {username}  Active Directory lockout and password state
}
WRITE_OPS = {
    "restart_service",  # {host, service}
    "cleanup_disk",  # {host, mount}  temp files, old logs, component store cleanup
    "renew_certificate",  # {host, certificate}  re-enroll from the issuing CA
    "unlock_account",  # {username}
}


class EdgeError(RuntimeError):
    pass


class EdgeClient:
    def __init__(self, url: str, token: str, client: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.http = client or httpx.Client(timeout=120)
        self.headers = {"Authorization": f"Bearer {token}"}

    def op(self, name: str, **args: Any) -> dict[str, Any]:
        if name not in READ_OPS | WRITE_OPS:
            raise ValueError(f"unknown edge operation {name!r}")
        r = self.http.post(f"{self.url}/v1/ops/{name}", json=args, headers=self.headers)
        r.raise_for_status()
        body = r.json()
        if not body.get("ok", False):
            raise EdgeError(body.get("error") or f"{name} failed")
        return body.get("result") or {}
