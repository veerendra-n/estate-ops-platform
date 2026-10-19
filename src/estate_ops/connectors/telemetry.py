"""Read-only telemetry for agents and skills: VictoriaMetrics (metrics) and VictoriaLogs (logs).

The OpenTelemetry pipeline tags every series and log line with tenant_id. Agents never write PromQL or
LogsQL: they pick a named metric and an asset, and this module fills a fixed template. Every metrics query
also carries extra_label=tenant_id=<tenant>, which VictoriaMetrics enforces on top of the template, and
every logs query starts with the tenant filter, so a query can't reach another customer's data.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

_ASSET = re.compile(r"^[A-Za-z0-9._:/ -]{1,128}$")
_PHRASE = re.compile(r"^[\w .:/-]{1,80}$")

# Named metrics an agent may ask for. $asset is the only hole, filled with a validated, quoted value.
METRICS: dict[str, tuple[str, str]] = {
    "host_cpu_utilization": ('max(host_cpu_utilization{host="$asset"})', "share of CPU in use, 0 to 1"),
    "host_memory_utilization": ('max(host_memory_utilization{host="$asset"})', "share of memory in use, 0 to 1"),
    "host_disk_used_ratio": ('max by (mount) (host_disk_used_ratio{host="$asset"})', "share of each volume in use"),
    "host_disk_latency_ms": ('max(host_disk_latency_ms{host="$asset"})', "disk latency the server sees"),
    "host_disk_iops": ('max(host_disk_iops{host="$asset"})', "disk operations per second the server drives"),
    "windows_service_up": ('min by (win_service) (windows_service_up{host="$asset"})', "1 if each Windows service runs"),
    "datastore_latency_ms": ('max(datastore_latency_ms{datastore="$asset"})', "datastore write latency"),
    "datastore_used_ratio": ('max(datastore_used_ratio{datastore="$asset"})', "share of the datastore in use"),
    "hypervisor_cpu_utilization": ('max(hypervisor_cpu_utilization{hypervisor="$asset"})', "host CPU in use"),
    "service_latency_p95_ms": ('max(service_latency_p95_ms{service="$asset"})', "95th percentile response time"),
    "service_error_ratio": ('max(service_error_ratio{service="$asset"})', "share of requests failing"),
    "backup_repository_free_ratio": ('min(backup_repository_free_ratio{repository="$asset"})', "free share of a repository"),
    "certificate_days_left": ('min by (certificate) (certificate_days_left{host="$asset"})', "days until expiry"),
    "vpn_tunnel_drops": ('sum(increase(vpn_tunnel_drops_total{device="$asset"}[15m]))', "VPN drops in 15 minutes"),
}


class TelemetryError(ValueError):
    pass


def _asset(value: str) -> str:
    if not _ASSET.match(value or ""):
        raise TelemetryError("asset must be a name like lab-sql01 or ds-shop-02")
    return value.replace("\\", "").replace('"', "")


def promql(metric: str, asset: str) -> str:
    try:
        template = METRICS[metric][0]
    except KeyError:
        raise TelemetryError(f"unknown metric {metric!r}; choose one of {sorted(METRICS)}") from None
    return template.replace("$asset", _asset(asset))


class Metrics:
    def __init__(self, url: str, tenant_id: str, client: httpx.Client | None = None):
        self.url, self.tenant_id = url.rstrip("/"), tenant_id
        self.http = client or httpx.Client(timeout=20)

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        r = self.http.get(f"{self.url}{path}", params={**params, "extra_label": f"tenant_id={self.tenant_id}"})
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise TelemetryError(body.get("error", "query failed"))
        return body["data"]["result"]

    def series(self, metric: str, asset: str, minutes: int = 60) -> dict[str, Any]:
        """A named metric for one asset over the last minutes, summarised: first, last, min, max, 12 points."""
        minutes = max(5, min(int(minutes), 1440))
        end = time.time()
        step = max(15, minutes * 60 // 60)
        result = self._get(
            "/api/v1/query_range", {"query": promql(metric, asset), "start": end - minutes * 60, "end": end, "step": step}
        )
        out = []
        for s in result:
            values = [float(v) for _, v in s.get("values", [])]
            labels = {k: v for k, v in s.get("metric", {}).items() if k not in ("tenant_id", "__name__")}
            out.append(
                {
                    "labels": labels,
                    "first": values[0] if values else None,
                    "last": values[-1] if values else None,
                    "min": min(values) if values else None,
                    "max": max(values) if values else None,
                    "points": [round(v, 4) for v in values[-12:]],
                }
            )
        return {"metric": metric, "asset": asset, "minutes": minutes, "meaning": METRICS[metric][1], "series": out}

    def latest(self, metric: str, asset: str) -> float | None:
        result = self._get("/api/v1/query", {"query": promql(metric, asset)})
        values = [float(s["value"][1]) for s in result if s.get("value")]
        return max(values) if values else None

    def forecast_hours_to(self, metric: str, asset: str, threshold: float, hours: int = 6) -> float | None:
        """Hours until a rising metric reaches threshold at its trend over the last `hours`, or None if it won't."""
        expr = f"deriv(({promql(metric, asset)})[{int(hours)}h:5m])"
        slope = self._get("/api/v1/query", {"query": expr})
        now = self.latest(metric, asset)
        if not slope or now is None:
            return None
        per_second = max(float(s["value"][1]) for s in slope)
        if per_second <= 0 or now >= threshold:
            return 0.0 if now is not None and now >= threshold else None
        return round((threshold - now) / per_second / 3600, 1)


class Logs:
    def __init__(self, url: str, tenant_id: str, client: httpx.Client | None = None):
        self.url, self.tenant_id = url.rstrip("/"), tenant_id
        self.http = client or httpx.Client(timeout=20)

    def search(self, asset: str, phrase: str, minutes: int = 60, limit: int = 20) -> dict[str, Any]:
        """Recent log lines from one asset containing a phrase."""
        if not _PHRASE.match(phrase or ""):
            raise TelemetryError("phrase must be plain words")
        minutes = max(5, min(int(minutes), 1440))
        query = f'tenant_id:="{self.tenant_id}" host:="{_asset(asset)}" _time:{minutes}m "{phrase}" | sort by (_time desc)'
        r = self.http.get(f"{self.url}/select/logsql/query", params={"query": query, "limit": max(1, min(limit, 50))})
        r.raise_for_status()
        lines = [json.loads(line) for line in r.text.splitlines() if line.strip()]
        lines = [x for x in lines if x.get("tenant_id") == self.tenant_id]  # belt and braces
        return {
            "asset": asset,
            "phrase": phrase,
            "count": len(lines),
            "lines": [{"at": x.get("_time"), "msg": str(x.get("_msg", ""))[:300]} for x in lines],
        }
