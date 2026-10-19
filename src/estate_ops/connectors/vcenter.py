"""VMware vCenter 8.x and 9.x through the vSphere Automation REST API.

Reads: clusters, hosts, datastores, VMs and where each VM runs. Writes (skills only, after policy):
relocate a VM to another datastore (storage vMotion) or host, and grow a virtual disk.
List calls stay under vCenter's per-call limits (4000 VMs, 2500 hosts) by asking per host.
Shapes not yet checked against a real vCenter are listed in docs/integrations.md.
"""

from __future__ import annotations

from typing import Any

import httpx


class VCenterClient:
    def __init__(self, url: str, username: str, password: str, client: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self._creds = (username, password)
        self.http = client or httpx.Client(timeout=60)
        self._session: str | None = None

    def _headers(self) -> dict[str, str]:
        if self._session is None:
            r = self.http.post(f"{self.url}/api/session", auth=self._creds)
            r.raise_for_status()
            self._session = r.json()
        return {"vmware-api-session-id": self._session}

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        r = self.http.request(method, f"{self.url}/api/{path}", headers=self._headers(), **kwargs)
        if r.status_code == 401:  # session expired: log in again once
            self._session = None
            r = self.http.request(method, f"{self.url}/api/{path}", headers=self._headers(), **kwargs)
        r.raise_for_status()
        return r.json() if r.content else None

    def get(self, path: str, **params: Any) -> Any:
        return self._call("GET", f"vcenter/{path}", params=params)

    def close(self) -> None:
        if self._session:
            try:
                self.http.delete(f"{self.url}/api/session", headers={"vmware-api-session-id": self._session})
            finally:
                self._session = None

    # ---- reads ----------------------------------------------------------------------------------------

    def clusters(self) -> list[dict[str, Any]]:
        return self.get("cluster")

    def hosts(self, cluster: str | None = None) -> list[dict[str, Any]]:
        return self.get("host", clusters=cluster) if cluster else self.get("host")

    def datastores(self) -> list[dict[str, Any]]:
        return self.get("datastore")

    def vms(self, host: str | None = None, datastore: str | None = None) -> list[dict[str, Any]]:
        params = {k: v for k, v in (("hosts", host), ("datastores", datastore)) if v}
        return self.get("vm", **params)

    def vm_by_name(self, name: str) -> dict[str, Any] | None:
        found = self.get("vm", names=name)
        return found[0] if found else None

    def vm(self, vm_id: str) -> dict[str, Any]:
        return self.get(f"vm/{vm_id}")

    # ---- actions (skills only) -------------------------------------------------------------------------

    def relocate(self, vm_id: str, datastore: str | None = None, host: str | None = None) -> None:
        placement = {k: v for k, v in (("datastore", datastore), ("host", host)) if v}
        self._call("POST", f"vcenter/vm/{vm_id}", params={"action": "relocate"}, json={"placement": placement})

    def grow_disk(self, vm_id: str, disk_id: str, capacity_bytes: int) -> None:
        self._call("PATCH", f"vcenter/vm/{vm_id}/hardware/disk/{disk_id}", json={"capacity": capacity_bytes})
