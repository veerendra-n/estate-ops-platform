"""Append-only, hash-chained action ledger. Each tenant has its own chain.

Every agent finding, proposal, approval and skill run is an entry, so the case conversation and the
audit trail are the same record. The database refuses updates and deletes; verify() detects tampering
done around it (for example by a superuser) by recomputing the chain.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

import psycopg

GENESIS = "0" * 64


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def entry_hash(prev_hash: str, tenant_id: str, case_id: str | None, actor: str, kind: str, body: dict) -> str:
    material = _canonical([prev_hash, tenant_id, case_id, actor, kind, body])
    return hashlib.sha256(material.encode()).hexdigest()


def append(
    conn: psycopg.Connection,
    tenant_id: str,
    case_id: UUID | str | None,
    actor: str,
    kind: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    # Serialise writers per tenant so the chain never forks.
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('ledger:' || %s))", (tenant_id,))
    row = conn.execute("SELECT hash FROM ledger WHERE tenant_id = %s ORDER BY id DESC LIMIT 1", (tenant_id,)).fetchone()
    prev = row["hash"] if row else GENESIS
    case = str(case_id) if case_id else None
    body = json.loads(_canonical(body))
    digest = entry_hash(prev, tenant_id, case, actor, kind, body)
    return conn.execute(
        "INSERT INTO ledger (tenant_id, case_id, actor, kind, body, prev_hash, hash) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id, at, hash",
        (tenant_id, case, actor, kind, json.dumps(body), prev, digest),
    ).fetchone()


def for_case(conn: psycopg.Connection, case_id: UUID | str) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT id, at, actor, kind, body, hash FROM ledger WHERE case_id = %s ORDER BY id", (str(case_id),)
    ).fetchall()


def verify(conn: psycopg.Connection, tenant_id: str) -> tuple[bool, int | None]:
    """Recompute the tenant's chain. Returns (ok, id of the first bad entry)."""
    prev = GENESIS
    rows = conn.execute(
        "SELECT id, case_id, actor, kind, body, prev_hash, hash FROM ledger WHERE tenant_id = %s ORDER BY id",
        (tenant_id,),
    )
    for r in rows:
        case = str(r["case_id"]) if r["case_id"] else None
        if r["prev_hash"] != prev or entry_hash(prev, tenant_id, case, r["actor"], r["kind"], r["body"]) != r["hash"]:
            return False, r["id"]
        prev = r["hash"]
    return True, None
