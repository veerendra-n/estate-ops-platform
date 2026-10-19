"""Fleet (open-source osquery manager): device inventory and live device queries.

Only named queries run: an agent or skill picks one of QUERIES, never writes SQL. Team ids keep one
customer's hosts apart when Fleet is shared.
"""

from __future__ import annotations

from typing import Any

import httpx

QUERIES: dict[str, str] = {
    "vpn_certificates": "SELECT common_name, not_valid_after, issuer FROM certificates "
    "WHERE path LIKE '%My%' AND common_name LIKE '%vpn%';",
    "vpn_profiles": "SELECT name, data FROM registry WHERE path LIKE "
    "'HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\RasMan\\Config\\%';",
    "disk_free": "SELECT path, ROUND(blocks_available * 100.0 / blocks, 1) AS free_pct FROM mounts WHERE path IN ('/', 'C:\\');",
    "uptime": "SELECT days, hours FROM uptime;",
    "crashing_apps": "SELECT module_path, count(*) AS crashes FROM windows_crashes "
    "GROUP BY module_path ORDER BY crashes DESC LIMIT 5;",
}


class FleetClient:
    def __init__(self, url: str, token: str, team_id: int | None = None, client: httpx.Client | None = None):
        self.url, self.team_id = url.rstrip("/"), team_id
        self.http = client or httpx.Client(timeout=60)
        self.headers = {"Authorization": f"Bearer {token}"}

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        r = self.http.request(method, f"{self.url}/api/v1/fleet/{path}", headers=self.headers, **kwargs)
        r.raise_for_status()
        return r.json() if r.content else None

    def hosts(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 0
        while True:
            params: dict[str, Any] = {"page": page, "per_page": 500}
            if self.team_id is not None:
                params["team_id"] = self.team_id
            batch = (self._call("GET", "hosts", params=params) or {}).get("hosts", [])
            out.extend(batch)
            if len(batch) < 500:
                return out
            page += 1

    def host_by_name(self, name: str) -> dict[str, Any] | None:
        try:
            return (self._call("GET", f"hosts/identifier/{name}") or {}).get("host")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    def query(self, host_id: int, name: str) -> list[dict[str, Any]]:
        """Run a named live query on one host and return its rows."""
        sql = QUERIES.get(name)
        if sql is None:
            raise ValueError(f"unknown query {name!r}; choose one of {sorted(QUERIES)}")
        body = self._call("POST", f"hosts/{host_id}/query", json={"query": sql}) or {}
        return body.get("rows", [])
