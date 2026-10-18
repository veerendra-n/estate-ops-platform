"""A small durable job queue in Postgres. Enough for one worker process per platform instance."""

from __future__ import annotations

import json
from typing import Any

import psycopg

from .db import Database

MAX_ATTEMPTS = 5


def enqueue(conn: psycopg.Connection, tenant_id: str, kind: str, payload: dict[str, Any], delay_s: float = 0) -> int:
    row = conn.execute(
        "INSERT INTO jobs (tenant_id, kind, payload, run_after) "
        "VALUES (%s, %s, %s, now() + make_interval(secs => %s)) RETURNING id",
        (tenant_id, kind, json.dumps(payload), delay_s),
    ).fetchone()
    return row["id"]


def claim(db: Database) -> dict[str, Any] | None:
    with db.unscoped() as conn:
        return conn.execute(
            """
            UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = now()
            WHERE id = (
                SELECT id FROM jobs WHERE status = 'queued' AND run_after <= now()
                ORDER BY run_after, id FOR UPDATE SKIP LOCKED LIMIT 1
            )
            RETURNING id, tenant_id, kind, payload, attempts
            """
        ).fetchone()


def finish(db: Database, job_id: int) -> None:
    with db.unscoped() as conn:
        conn.execute("UPDATE jobs SET status = 'done', updated_at = now() WHERE id = %s", (job_id,))


def fail(db: Database, job: dict[str, Any], error: str) -> bool:
    """Requeue with backoff, or mark failed after MAX_ATTEMPTS. Returns True when it will retry."""
    retry = job["attempts"] < MAX_ATTEMPTS
    with db.unscoped() as conn:
        conn.execute(
            "UPDATE jobs SET status = %s, last_error = %s, updated_at = now(), "
            "run_after = now() + make_interval(secs => %s) WHERE id = %s",
            ("queued" if retry else "failed", error[:2000], 2 ** job["attempts"], job["id"]),
        )
    return retry


def reschedule(db: Database, job: dict[str, Any], payload: dict[str, Any], delay_s: float) -> None:
    """Put a job back without counting an attempt (used to poll a running runbook)."""
    with db.unscoped() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'queued', attempts = attempts - 1, payload = %s, updated_at = now(), "
            "run_after = now() + make_interval(secs => %s) WHERE id = %s",
            (json.dumps(payload), delay_s, job["id"]),
        )
