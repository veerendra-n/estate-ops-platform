"""The estate graph: what each customer runs and how it depends on each other.

Stored as assets and relations in Postgres. Neighbourhood queries are a recursive CTE, which is plenty at
reference-customer scale (about 300 servers and 50 apps). A graph database can replace this module later.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
import yaml


def load_seed(conn: psycopg.Connection, path: Path) -> int:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    tenant, source = raw["tenant"], raw.get("source", "seed")
    for a in raw.get("assets", []):
        conn.execute(
            "INSERT INTO assets (tenant_id, id, kind, name, attrs, source) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (tenant_id, id) DO UPDATE SET kind = EXCLUDED.kind, name = EXCLUDED.name, "
            "attrs = EXCLUDED.attrs, source = EXCLUDED.source, seen_at = now()",
            (tenant, a["id"], a["kind"], a["name"], json.dumps(a.get("attrs", {})), source),
        )
    for src, rel, dst in raw.get("relations", []):
        conn.execute(
            "INSERT INTO relations (tenant_id, src, rel, dst, source) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (tenant, src, rel, dst, source),
        )
    return len(raw.get("assets", []))


def host_asset(conn: psycopg.Connection, tenant_id: str, host: str) -> dict[str, Any] | None:
    """The server asset for an inventory host name, only within this tenant."""
    return conn.execute(
        "SELECT id, kind, name, attrs FROM assets WHERE tenant_id = %s AND kind = 'server' AND name = %s",
        (tenant_id, host),
    ).fetchone()


def neighbourhood(conn: psycopg.Connection, tenant_id: str, asset_id: str, hops: int = 3) -> dict[str, Any]:
    """Assets within `hops` relations of asset_id in either direction, with the relations between them."""
    hops = max(1, min(hops, 4))
    ids = conn.execute(
        """
        WITH RECURSIVE walk(id, depth) AS (
            SELECT %(start)s::text, 0
          UNION
            SELECT CASE WHEN r.src = w.id THEN r.dst ELSE r.src END, w.depth + 1
            FROM walk w JOIN relations r
              ON r.tenant_id = %(tenant)s AND (r.src = w.id OR r.dst = w.id)
            WHERE w.depth < %(hops)s
        )
        SELECT id, min(depth) AS depth FROM walk GROUP BY id
        """,
        {"start": asset_id, "tenant": tenant_id, "hops": hops},
    ).fetchall()
    id_list = [r["id"] for r in ids]
    assets = conn.execute(
        "SELECT id, kind, name, attrs FROM assets WHERE tenant_id = %s AND id = ANY(%s) ORDER BY kind, id",
        (tenant_id, id_list),
    ).fetchall()
    relations = conn.execute(
        "SELECT src, rel, dst FROM relations WHERE tenant_id = %s AND src = ANY(%s) AND dst = ANY(%s) ORDER BY src",
        (tenant_id, id_list, id_list),
    ).fetchall()
    return {"centre": asset_id, "assets": assets, "relations": relations}
