"""The estate graph in Neo4j Community: what each customer runs and how it depends on each other.

Community edition has one database and no per-database access control, so isolation is done here:
every node carries tenant_id, every query matches on it, and nothing in this module accepts a query
from outside. Node identity is uid = "<tenant_id>|<asset id>", kept unique by a constraint.

Several sources can report the same asset (vCenter, Veeam and the customer's declared services all know
server:CTO-SHOP-SQL01). A node keeps the list of sources that report it; each source's run adds its view and
prunes only its own name, and the node goes when no source reports it any more.

Relations point from what depends to what it depends on: service -runs_on-> server -stored_on-> datastore,
server -runs_on-> hypervisor_host -member_of-> hypervisor_cluster, backup_job -protects-> server and
-writes_to-> backup_repository. Asset ids are "<kind>:<name>".
"""

from __future__ import annotations

from typing import Any

from neo4j import GraphDatabase

SCHEMA = [
    "CREATE CONSTRAINT asset_uid IF NOT EXISTS FOR (a:Asset) REQUIRE a.uid IS UNIQUE",
    "CREATE INDEX asset_tenant_kind_name IF NOT EXISTS FOR (a:Asset) ON (a.tenant_id, a.kind, a.name)",
]
# Kinds in the order the console lays them out, from what people use down to what it runs on.
LAYERS = (
    ("service", "application", "m365_service"),
    ("database", "sql_instance", "sql_agent_job", "backup_job"),
    ("server", "endpoint", "cloud_resource"),
    ("hypervisor_host", "hypervisor_cluster", "network_device", "device"),
    ("datastore", "backup_repository", "storage_array", "power"),
)
LAYER_OF = {kind: i for i, kinds in enumerate(LAYERS) for kind in kinds}
# Kinds that connect too much to say two alerts are related when a path passes through them.
HUBS = ("hypervisor_cluster", "hypervisor_host", "network_device")


def _uid(tenant_id: str, asset_id: str) -> str:
    return f"{tenant_id}|{asset_id}"


def _asset(node: Any) -> dict[str, Any]:
    props = dict(node)
    attrs = {k[5:]: v for k, v in props.items() if k.startswith("attr_")}
    seen = props.get("seen_at")
    return {
        "id": props["id"],
        "kind": props["kind"],
        "name": props["name"],
        "attrs": attrs,
        "sources": list(props.get("sources") or []),
        "seen_at": seen.iso_format() if hasattr(seen, "iso_format") else seen,
    }


class EstateGraph:
    def __init__(self, url: str, user: str, password: str | None):
        self.driver = GraphDatabase.driver(url, auth=(user, password or ""), notifications_min_severity="WARNING")

    def close(self) -> None:
        self.driver.close()

    def ensure_schema(self) -> None:
        for stmt in SCHEMA:
            self.driver.execute_query(stmt)

    def upsert(self, tenant_id: str, source: str, assets: list[dict], relations: list[list[str]]) -> int:
        rows = [
            {
                "uid": _uid(tenant_id, a["id"]),
                "props": {"tenant_id": tenant_id, "id": a["id"], "kind": a["kind"], "name": a["name"]}
                | {f"attr_{k}": v for k, v in (a.get("attrs") or {}).items() if v is not None},
            }
            for a in assets
        ]
        # Attributes merge across sources (the latest report of an attribute wins); kind and name too.
        self.driver.execute_query(
            "UNWIND $rows AS row MERGE (a:Asset {uid: row.uid}) ON CREATE SET a.sources = [] "
            "SET a += row.props, a.seen_at = datetime(), "
            "a.sources = CASE WHEN $source IN a.sources THEN a.sources ELSE a.sources + $source END",
            rows=rows,
            source=source,
        )
        rels = [{"src": _uid(tenant_id, s), "rel": r, "dst": _uid(tenant_id, d)} for s, r, d in relations]
        self.driver.execute_query(
            "UNWIND $rels AS r MATCH (s:Asset {uid: r.src, tenant_id: $tenant}) MATCH (d:Asset {uid: r.dst, tenant_id: $tenant}) "
            "MERGE (s)-[:REL {type: r.rel, source: $source}]->(d)",
            rels=rels,
            tenant=tenant_id,
            source=source,
        )
        return len(assets)

    def drop_relations(self, tenant_id: str, source: str) -> None:
        """Remove the relations one source wrote, before it writes its current view again."""
        self.driver.execute_query(
            "MATCH (:Asset {tenant_id: $tenant})-[r:REL {source: $source}]->(:Asset {tenant_id: $tenant}) DELETE r",
            tenant=tenant_id,
            source=source,
        )

    def prune(self, tenant_id: str, source: str, keep_ids: list[str]) -> int:
        """Forget this source's assets that it no longer reports; delete those no source reports. Returns how many went."""
        records, _, _ = self.driver.execute_query(
            "MATCH (a:Asset {tenant_id: $tenant}) WHERE $source IN a.sources AND NOT a.uid IN $keep "
            "SET a.sources = [s IN a.sources WHERE s <> $source] "
            "WITH a WHERE size(a.sources) = 0 DETACH DELETE a RETURN count(*) AS n",
            tenant=tenant_id,
            source=source,
            keep=[_uid(tenant_id, i) for i in keep_ids],
        )
        return records[0]["n"]

    def assets(self, tenant_id: str, query: str = "", kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Assets of one tenant whose name or id contains the query, for the console's inventory."""
        records, _, _ = self.driver.execute_query(
            "MATCH (a:Asset {tenant_id: $tenant}) WHERE ($kind IS NULL OR a.kind = $kind) "
            "AND ($q = '' OR toLower(a.name) CONTAINS $q OR toLower(a.id) CONTAINS $q) "
            "RETURN a ORDER BY a.kind, a.name LIMIT $limit",
            tenant=tenant_id,
            kind=kind,
            q=query.strip().lower(),
            limit=max(1, min(int(limit), 1000)),
        )
        return [_asset(r["a"]) for r in records]

    def asset(self, tenant_id: str, asset_id: str) -> dict[str, Any] | None:
        records, _, _ = self.driver.execute_query(
            "MATCH (a:Asset {uid: $uid, tenant_id: $tenant}) RETURN a", uid=_uid(tenant_id, asset_id), tenant=tenant_id
        )
        return _asset(records[0]["a"]) if records else None

    def find(self, tenant_id: str, name: str) -> dict[str, Any] | None:
        """The asset an alert's target names: a server first, then anything with that name."""
        records, _, _ = self.driver.execute_query(
            "MATCH (a:Asset {tenant_id: $tenant}) WHERE toLower(a.name) = toLower($name) "
            "RETURN a ORDER BY CASE a.kind WHEN 'server' THEN 0 WHEN 'endpoint' THEN 1 ELSE 2 END LIMIT 1",
            tenant=tenant_id,
            name=name,
        )
        return _asset(records[0]["a"]) if records else None

    def stats(self, tenant_id: str) -> dict[str, Any]:
        """How many assets of each kind, and how many each source reports."""
        kinds, _, _ = self.driver.execute_query(
            "MATCH (a:Asset {tenant_id: $tenant}) RETURN a.kind AS kind, count(*) AS n ORDER BY n DESC", tenant=tenant_id
        )
        sources, _, _ = self.driver.execute_query(
            "MATCH (a:Asset {tenant_id: $tenant}) UNWIND a.sources AS s RETURN s AS source, count(*) AS n ORDER BY n DESC",
            tenant=tenant_id,
        )
        return {
            "total": sum(r["n"] for r in kinds),
            "kinds": {r["kind"]: r["n"] for r in kinds},
            "sources": {r["source"]: r["n"] for r in sources},
        }

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
        # Two sources reporting the same relation write one edge each; show it once.
        unique = {(rel["src"], rel["rel"], rel["dst"]) for r in records for rel in r["out"] if rel["rel"] is not None}
        relations = [{"src": s, "rel": t, "dst": d} for s, t, d in sorted(unique, key=lambda x: (x[0], x[2], x[1]))]
        return {"centre": asset_id, "assets": assets, "relations": relations}

    def snapshot(self, tenant_id: str, source: str) -> dict[str, dict[str, Any]]:
        """What one source reported last time: asset id -> {name, kind, attrs}. Discovery diffs against it."""
        records, _, _ = self.driver.execute_query(
            "MATCH (a:Asset {tenant_id: $tenant}) WHERE $source IN a.sources RETURN a", tenant=tenant_id, source=source
        )
        return {a["id"]: a for a in (_asset(r["a"]) for r in records)}

    def kinds(self, tenant_id: str, asset_ids: list[str]) -> dict[str, str]:
        records, _, _ = self.driver.execute_query(
            "MATCH (a:Asset {tenant_id: $tenant}) WHERE a.uid IN $uids RETURN a.id AS id, a.kind AS kind",
            tenant=tenant_id,
            uids=[_uid(tenant_id, i) for i in asset_ids],
        )
        return {r["id"]: r["kind"] for r in records}

    def related(self, tenant_id: str, these: list[str], those: list[str], hops: int = 2) -> bool:
        """Whether any asset in `these` is within `hops` relations of one in `those`, not counting paths through
        hubs (two VMs on one hypervisor host are not related because of it). Triage uses this to fold alerts."""
        if not these or not those:
            return False
        hops = max(1, min(int(hops), 3))
        records, _, _ = self.driver.execute_query(
            f"MATCH p = (a:Asset {{tenant_id: $tenant}})-[:REL*0..{hops}]-(b:Asset {{tenant_id: $tenant}}) "
            "WHERE a.uid IN $a AND b.uid IN $b "
            "AND none(n IN nodes(p)[1..-1] WHERE n.kind IN $hubs) RETURN 1 LIMIT 1",
            tenant=tenant_id,
            a=[_uid(tenant_id, i) for i in these],
            b=[_uid(tenant_id, i) for i in those],
            hubs=list(HUBS),
        )
        return bool(records)

    def blast_radius(self, tenant_id: str, asset_ids: list[str]) -> dict[str, Any]:
        """The business services that depend on these assets (up to three relations up), for policy and people."""
        # A backup job's radius is the services of the servers it protects.
        records, _, _ = self.driver.execute_query(
            "MATCH (a:Asset {tenant_id: $tenant}) WHERE a.uid IN $uids "
            "OPTIONAL MATCH (a)-[:REL {type: 'protects'}]->(p:Asset {tenant_id: $tenant}) "
            "WITH collect(DISTINCT a) + collect(DISTINCT p) AS targets UNWIND targets AS t "
            "MATCH (s:Asset {tenant_id: $tenant, kind: 'service'})-[:REL*0..3]->(t) RETURN DISTINCT s",
            tenant=tenant_id,
            uids=[_uid(tenant_id, i) for i in asset_ids],
        )
        services = [_asset(r["s"]) for r in records]
        dependents, _, _ = self.driver.execute_query(
            "MATCH (d:Asset {tenant_id: $tenant})-[:REL*1..2]->(a:Asset {tenant_id: $tenant}) WHERE a.uid IN $uids "
            "RETURN count(DISTINCT d) AS n",
            tenant=tenant_id,
            uids=[_uid(tenant_id, i) for i in asset_ids],
        )
        return {
            "dependents": dependents[0]["n"] if dependents else 0,
            "services": sorted(s["name"] for s in services),
            "critical": any(bool(s["attrs"].get("critical")) for s in services),
            "users": sum(int(s["attrs"].get("users") or 0) for s in services),
        }
