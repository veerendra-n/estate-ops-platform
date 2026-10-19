"""Microsoft Graph with an app registration in each customer's Entra tenant (client credentials).

Reads: Intune managed devices, Entra users and their sign-in state, Microsoft 365 service health.
Writes (skills only): sync an Intune device, run a proactive remediation on demand, revoke a user's
sessions. Permissions: DeviceManagementManagedDevices.Read.All and .PrivilegedOperations.All,
User.Read.All, ServiceHealth.Read.All; the secret is named per tenant (client_secret_env).
"""

from __future__ import annotations

import time
from typing import Any

import httpx

DEVICE_FIELDS = (
    "id,deviceName,operatingSystem,osVersion,complianceState,lastSyncDateTime,userPrincipalName,"
    "serialNumber,manufacturer,model,managementAgent"
)


class GraphClient:
    def __init__(
        self,
        entra_tenant_id: str,
        client_id: str,
        client_secret: str,
        graph_url: str = "https://graph.microsoft.com",
        login_url: str = "https://login.microsoftonline.com",
        client: httpx.Client | None = None,
    ):
        self.graph_url, self.login_url = graph_url.rstrip("/"), login_url.rstrip("/")
        self.entra_tenant_id, self._creds = entra_tenant_id, (client_id, client_secret)
        self.http = client or httpx.Client(timeout=60)
        self._token: tuple[str, float] | None = None

    def _auth(self) -> dict[str, str]:
        if not self._token or self._token[1] < time.time() + 120:
            r = self.http.post(
                f"{self.login_url}/{self.entra_tenant_id}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._creds[0],
                    "client_secret": self._creds[1],
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
            r.raise_for_status()
            body = r.json()
            self._token = (body["access_token"], time.time() + float(body.get("expires_in", 3600)))
        return {"Authorization": f"Bearer {self._token[0]}"}

    def _call(self, method: str, path: str, version: str = "v1.0", **kwargs: Any) -> Any:
        r = self.http.request(method, f"{self.graph_url}/{version}/{path.lstrip('/')}", headers=self._auth(), **kwargs)
        r.raise_for_status()
        return r.json() if r.content else None

    def list(self, path: str, params: dict[str, Any] | None = None, limit: int = 20000) -> list[dict[str, Any]]:
        """Every item of a collection, following @odata.nextLink."""
        out: list[dict[str, Any]] = []
        url: str | None = f"{self.graph_url}/v1.0/{path.lstrip('/')}"
        query = params
        while url and len(out) < limit:
            r = self.http.get(url, params=query, headers=self._auth() | {"Prefer": "odata.maxpagesize=999"})
            r.raise_for_status()
            page = r.json()
            out.extend(page.get("value", []))
            url, query = page.get("@odata.nextLink"), None
        return out

    # ---- reads ----------------------------------------------------------------------------------------

    def devices(self) -> list[dict[str, Any]]:
        return self.list("deviceManagement/managedDevices", {"$select": DEVICE_FIELDS})

    def device_by_name(self, name: str) -> dict[str, Any] | None:
        safe = name.replace("'", "''")
        found = self.list("deviceManagement/managedDevices", {"$filter": f"deviceName eq '{safe}'", "$select": DEVICE_FIELDS})
        return found[0] if found else None

    def devices_of(self, upn: str) -> list[dict[str, Any]]:
        safe = upn.replace("'", "''")
        return self.list(
            "deviceManagement/managedDevices", {"$filter": f"userPrincipalName eq '{safe}'", "$select": DEVICE_FIELDS}
        )

    def user(self, upn: str) -> dict[str, Any] | None:
        try:
            return self._call(
                "GET", f"users/{upn}", params={"$select": "id,displayName,userPrincipalName,accountEnabled,mail,jobTitle"}
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    def service_health(self) -> list[dict[str, Any]]:
        return self.list("admin/serviceAnnouncement/healthOverviews")

    def service_issues(self) -> list[dict[str, Any]]:
        return self.list("admin/serviceAnnouncement/issues")

    # ---- actions (skills only) -------------------------------------------------------------------------

    def sync_device(self, device_id: str) -> None:
        self._call("POST", f"deviceManagement/managedDevices/{device_id}/syncDevice")

    def run_remediation(self, device_id: str, script_policy_id: str) -> None:
        # On-demand proactive remediation is in Graph beta only.
        self._call(
            "POST",
            f"deviceManagement/managedDevices/{device_id}/initiateOnDemandProactiveRemediation",
            version="beta",
            json={"scriptPolicyId": script_policy_id},
        )

    def revoke_sessions(self, upn: str) -> None:
        self._call("POST", f"users/{upn}/revokeSignInSessions")


def lower(value: Any) -> str:
    return str(value or "").lower()  # Graph samples send enums as PascalCase, the schema as camelCase
