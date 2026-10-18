"""The estate graph in Neo4j Community: what each customer runs and how it depends on each other.

Community edition has one database and no per-database access control, so isolation is done here:
every node carries tenant_id, every query matches on it, and nothing in this module accepts a query
from outside. Node identity is uid = "<tenant_id>|<asset id>", kept unique by a constraint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from neo4j import GraphDatabase

SCHEMA = [
    "CREATE CONSTRAINT asset_uid IF NOT EXISTS FOR (a:Asset) REQUIRE a.uid IS UNIQUE",
    "CREATE INDEX asset_tenant_kind_name IF NOT EXISTS FOR (a:Asset) ON (a.tenant_id, a.kind, a.name)",
]


def _uid(tenant_id: str, asset_id: str) -> str:
    return f"{tenant_id}|{asset_id}"


def _asset(node: Any) -> dict[str, Any]:
    props = dict(node)
    attrs = {k[5:]: v for k, v in props.items() if k.startswith("attr_")}
    return {"id": props["id"], "kind": props["kind"], "name": props["name"], "attrs": attrs}


class EstateGraph:
    def __init__(self, url: str, user: str, password: str | None):
        self.driver = GraphDatabase.driver(url, auth=(user, password or ""), notifications_min_severity="WARNING")

    def close(self) -> None:
        self.driver.close()

    def ensure_schema(self) -> None:
        for stmt in SCHEMA:
            self.driver.execute_query(stmt)

    def load_seed(self, path: Path) -> int:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        tenant, source = raw["tenant"], raw.get("source", "seed")
        return self.upsert(tenant, source, raw.get("assets", []), raw.get("relations", []))

    def upsert(self, tenant_id: str, source: str, assets: list[dict], relations: list[list[str]]) -> int:
        rows = [
            {
                "uid": _uid(tenant_id, a["id"]),
                "props": {"tenant_id": tenant_id, "id": a["id"], "kind": a["kind"], "name": a["name"], "source": source}
                | {f"attr_{k}": v for k, v in (a.get("attrs") or {}).items()},
            }
            for a in assets
        ]
        self.driver.execute_query(
            "UNWIND $rows AS row MERGE (a:Asset {uid: row.uid}) SET a = row.props, a.uid = row.uid, a.seen_at = datetime()",
            rows=rows,
        )
        rels = [{"src": _uid(tenant_id, s), "rel": r, "dst": _uid(tenant_id, d)} for s, r, d in relations]
        self.driver.execute_query(
            "UNWIND $rels AS r MATCH (s:Asset {uid: r.src, tenant_id: $tenant}) MATCH (d:Asset {uid: r.dst, tenant_id: $tenant}) "
            "MERGE (s)-[x:REL {type: r.rel}]->(d) SET x.source = $source",
            rels=rels,
            tenant=tenant_id,
            source=source,
        )
        return len(assets)

    def host(self, tenant_id: str, host: str) -> dict[str, Any] | None:
        """The server asset for an inventory host name, only within this tenant."""
        records, _, _ = self.driver.execute_query(
            "MATCH (a:Asset {tenant_id: $tenant, kind: 'server', name: $name}) RETURN a LIMIT 1",
            tenant=tenant_id,
            name=host,
        )
        return _asset(records[0]["a"]) if records else None

    def neighbourhood(self, tenant_id: str, asset_id: str, hops: int = 3) -> dict[str, Any]:
        """Assets within `hops` relations of asset_id in either direction, with the relations between them."""
        hops = max(1, min(int(hops), 4))
        records, _, _ = self.driver.execute_query(
            f"MATCH (c:Asset {{uid: $uid, tenant_id: $tenant}}) "
            f"OPTIONAL MATCH (c)-[:REL*1..{hops}]-(n:Asset {{tenant_id: $tenant}}) "
            "WITH c, collect(DISTINCT n) + c AS nodes "
            "UNWIND nodes AS a "
            "OPTIONAL MATCH (a)-[r:REL]->(b:Asset {tenant_id: $tenant}) WHERE b IN nodes "
            "RETURN a, collect({src: a.id, rel: r.type, dst: b.id}) AS out",
            uid=_uid(tenant_id, asset_id),
            tenant=tenant_id,
        )
        assets = sorted((_asset(r["a"]) for r in records), key=lambda a: (a["kind"], a["id"]))
        relations = sorted(
            (rel for r in records for rel in r["out"] if rel["rel"] is not None), key=lambda x: (x["src"], x["dst"])
        )
        return {"centre": asset_id, "assets": assets, "relations": relations}
