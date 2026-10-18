"""Send a sample OpenObserve alert to the platform, as OpenObserve's webhook destination would.

python dev/fire_alert.py                       # SQL log full on lab-sql01 (P2)
python dev/fire_alert.py print-spooler-stopped lab-prt01
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

ROWS = {
    "sql-log-used-high": {"sqlserver_database_name": "FinanceERP", "used_pct": 96.4},
    "print-spooler-stopped": {"service": "Spooler"},
    "disk-free-low": {"volume": "D:", "free_pct": 4.2},
}


def main() -> None:
    alert = sys.argv[1] if len(sys.argv) > 1 else "sql-log-used-high"
    host = sys.argv[2] if len(sys.argv) > 2 else "lab-sql01"
    tenant = os.environ.get("TENANT", "lab")
    token = os.environ.get(f"{tenant.upper()}_INTAKE_TOKEN", "change-me-lab-intake")
    url = os.environ.get("PLATFORM_URL", "http://localhost:8080")
    body = {"alert_id": alert, "org": tenant, "rows": [{"tenant_id": tenant, "host_name": host, **ROWS.get(alert, {})}]}
    req = urllib.request.Request(  # noqa: S310 (local dev URL)
        f"{url}/api/v1/intake/openobserve/{tenant}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (local dev URL)
        print(resp.status, resp.read().decode())


if __name__ == "__main__":
    main()
