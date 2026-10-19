"""Postgres access. Every unit of work runs inside one tenant scope, which row-level security enforces."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

MIGRATIONS = Path(__file__).parent / "migrations"


def migrate(database_url: str) -> list[str]:
    """Apply pending migrations as the database owner. Returns the names applied."""
    applied: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, at timestamptz DEFAULT now())")
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
    return applied


class Database:
    def __init__(self, database_url: str, min_size: int = 1, max_size: int = 5):
        # SET ROLE drops owner rights, so a query that forgets its tenant filter still sees only its scope.
        self.pool = ConnectionPool(
            database_url,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "autocommit": False},
            configure=lambda c: (c.execute("SET ROLE estate_app"), c.commit()),
            open=True,
        )

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def scope(self, tenants: str | Sequence[str]) -> Iterator[psycopg.Connection]:
        """One transaction limited to the given tenant(s)."""
        ids = [tenants] if isinstance(tenants, str) else list(tenants)
        if not ids or any("," in t for t in ids):
            raise ValueError("a scope needs one or more tenant ids")
        with self.pool.connection() as conn:
            with conn.transaction():
                conn.execute("SELECT set_config('app.tenants', %s, true)", (",".join(ids),))
                yield conn

    @contextmanager
    def unscoped(self) -> Iterator[psycopg.Connection]:
        """For the tenant list only; tenant tables return nothing here."""
        with self.pool.connection() as conn:
            with conn.transaction():
                conn.execute("SELECT set_config('app.tenants', '', true)")
                yield conn
