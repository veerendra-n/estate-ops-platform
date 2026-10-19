"""Veeam Backup & Replication v12 and v13 through its REST API (port 9419).

Reads: jobs (with the VMs they include), sessions, repository capacity. Writes (skills only): start a job,
remove orphaned backups (backups whose job no longer exists) from a repository. A Veeam user in the
Backup Operator role covers both; discovery and alerts need only Backup Viewer.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

API_VERSION = "1.2-rev0"  # 12.3; 13.x accepts it too (the API is backward compatible)


class VeeamClient:
    def __init__(
        self, url: str, username: str, password: str, api_version: str = API_VERSION, client: httpx.Client | None = None
    ):
        self.url = url.rstrip("/")
        self._creds = (username, password)
        self.api_version = api_version
        self.http = client or httpx.Client(timeout=60)
        self._token: tuple[str, float] | None = None

    def _auth(self) -> dict[str, str]:
        if not self._token or self._token[1] < time.time() + 60:
            r = self.http.post(
                f"{self.url}/api/oauth2/token",
                data={"grant_type": "password", "username": self._creds[0], "password": self._creds[1]},
                headers={"x-api-version": self.api_version},
            )
            r.raise_for_status()
            body = r.json()
            self._token = (body["access_token"], time.time() + float(body.get("expires_in", 900)))
        return {"Authorization": f"Bearer {self._token[0]}", "x-api-version": self.api_version}

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        r = self.http.request(method, f"{self.url}/api/v1/{path}", headers=self._auth(), **kwargs)
        r.raise_for_status()
        return r.json() if r.content else None

    def list(self, path: str, **params: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        skip, limit = 0, 200
        while True:
            page = (self._call("GET", path, params={**params, "skip": skip, "limit": limit}) or {}).get("data", [])
            out.extend(page)
            if len(page) < limit or len(out) >= 10000:
                return out
            skip += limit

    def jobs(self) -> list[dict[str, Any]]:
        return self.list("jobs")

    def job_by_name(self, name: str) -> dict[str, Any] | None:
        return next((j for j in self.list("jobs", nameFilter=name) if j.get("name") == name), None)

    def sessions(self, **filters: Any) -> list[dict[str, Any]]:
        return self.list("sessions", **filters)

    def repositories(self) -> list[dict[str, Any]]:
        """Repository states: capacityGB, freeGB, usedSpaceGB per repository."""
        return self.list("backupInfrastructure/repositories/states")

    def backups(self, repository_id: str | None = None) -> list[dict[str, Any]]:
        backups = self.list("backups")
        return [b for b in backups if not repository_id or b.get("repositoryId") == repository_id]

    def start_job(self, job_id: str) -> dict[str, Any]:
        return self._call("POST", f"jobs/{job_id}/start") or {}

    def remove_backup(self, backup_id: str) -> None:
        self._call("POST", f"backups/{backup_id}/removeFromDisk")

    def session(self, session_id: str) -> dict[str, Any]:
        return self._call("GET", f"sessions/{session_id}")


def result_of(session: dict[str, Any]) -> str:
    result = session.get("result")
    return str((result or {}).get("result") if isinstance(result, dict) else result or "").lower()
