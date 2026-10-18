"""Read-only telemetry queries against OpenObserve, one organisation per tenant.

Agents never write SQL. They pick a stream and a host; this module builds the query from a fixed template
and only for the tenant's own organisation, then returns a small summary instead of raw rows.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

_IDENT = re.compile(r"^[a-z0-9_]{1,64}$")
_HOST = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class OpenObserve:
    def __init__(self, base_url: str, user: str | None, password: str | None, client: httpx.Client | None = None):
        self.http = client or httpx.Client(base_url=base_url, timeout=20.0)
        if user and password:
            self.http.auth = httpx.BasicAuth(user, password)

    def _search(self, org: str, sql: str, minutes: int, size: int) -> list[dict[str, Any]]:
        end = int(time.time() * 1_000_000)
        start = end - minutes * 60 * 1_000_000
        resp = self.http.post(
            f"/api/{org}/_search",
            json={"query": {"sql": sql, "start_time": start, "end_time": end, "size": size}},
        )
        resp.raise_for_status()
        return resp.json().get("hits", [])

    def metric_series(self, org: str, stream: str, host: str, minutes: int = 60) -> dict[str, Any]:
        if not _IDENT.match(stream) or not _HOST.match(host):
            raise ValueError("stream must be a metric stream name and host a host name")
        minutes = max(5, min(minutes, 1440))
        sql = (
            f"SELECT histogram(_timestamp, '5 minutes') AS t, MAX(value) AS max_value FROM \"{stream}\" "
            f"WHERE host_name = '{host}' GROUP BY t ORDER BY t"
        )
        hits = self._search(org, sql, minutes, 300)
        values = [h.get("max_value") for h in hits if h.get("max_value") is not None]
        return {
            "stream": stream,
            "host": host,
            "minutes": minutes,
            "points": len(values),
            "first": values[0] if values else None,
            "last": values[-1] if values else None,
            "max": max(values) if values else None,
            "series": hits[-12:],
        }

    def log_lines(self, org: str, host: str, contains: str, minutes: int = 60) -> dict[str, Any]:
        if not _HOST.match(host) or not re.match(r"^[\w .:-]{1,80}$", contains):
            raise ValueError("host must be a host name and contains plain words")
        minutes = max(5, min(minutes, 1440))
        needle = contains.replace("'", "")
        sql = (
            'SELECT _timestamp, body FROM "default" '
            f"WHERE host_name = '{host}' AND str_match_ignore_case(body, '{needle}') ORDER BY _timestamp DESC"
        )
        hits = self._search(org, sql, minutes, 20)
        return {"host": host, "contains": contains, "count": len(hits), "lines": [str(h.get("body", ""))[:300] for h in hits]}
