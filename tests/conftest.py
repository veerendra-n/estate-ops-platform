"""Tests run against a real Postgres (row-level security and the ledger trigger are part of what's tested).

Set TEST_DATABASE_URL to a server where the user may create databases; each test gets a fresh database
cloned from a migrated, seeded template. Semaphore and OpenObserve are the in-process fakes from dev/.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from dev import fakes  # noqa: E402
from estate_ops import db as dbmod  # noqa: E402
from estate_ops import estate  # noqa: E402
from estate_ops.catalog import Catalog  # noqa: E402
from estate_ops.config import Settings  # noqa: E402
from estate_ops.db import Database  # noqa: E402
from estate_ops.semaphore import Semaphore  # noqa: E402
from estate_ops.service import Platform  # noqa: E402
from estate_ops.telemetry import OpenObserve  # noqa: E402
from estate_ops.tenancy import Tenants  # noqa: E402

BASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@localhost:5432/postgres")
TEMPLATE = "estate_ops_test_template"


def _url(dbname: str) -> str:
    return psycopg.conninfo.make_conninfo(BASE_URL, dbname=dbname)


@pytest.fixture(scope="session")
def template_db():
    tenants = Tenants.load(ROOT / "config/tenants.yml")
    with psycopg.connect(BASE_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEMPLATE} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {TEMPLATE}")
    dbmod.migrate(_url(TEMPLATE))
    tenants.sync(_url(TEMPLATE))
    with psycopg.connect(_url(TEMPLATE)) as conn:
        estate.load_seed(conn, ROOT / "config/seed/lab.yml")
        # A second customer with a server of the same kind, to prove isolation.
        conn.execute(
            "INSERT INTO assets (tenant_id, id, kind, name, attrs, source) VALUES "
            "('contoso', 'host:cto-sql01', 'server', 'cto-sql01', '{}', 'test'), "
            "('contoso', 'host:cto-prt01', 'server', 'cto-prt01', '{}', 'test')"
        )
    yield TEMPLATE
    with psycopg.connect(BASE_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEMPLATE} WITH (FORCE)")


@pytest.fixture
def database_url(template_db):
    name = f"estate_ops_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(BASE_URL, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name} TEMPLATE {template_db}")
    yield _url(name)
    with psycopg.connect(BASE_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def fakes_client():
    fakes.clear_scenarios()
    fakes._tasks.clear()
    return TestClient(fakes.app)


@pytest.fixture
def make_platform(database_url, fakes_client, monkeypatch):
    monkeypatch.setenv("LAB_INTAKE_TOKEN", "lab-test-token")
    monkeypatch.setenv("CONTOSO_INTAKE_TOKEN", "contoso-test-token")
    monkeypatch.setenv("CONSOLE_TOKEN", "console-test-token")
    made = []

    def _make(anthropic=None, tenants_file=None) -> Platform:
        settings = Settings(
            database_url=database_url,
            tenants_file=tenants_file or ROOT / "config/tenants.yml",
            catalog_file=ROOT / "tests/fixtures/catalog.json",
            alerts_dir=None,
            seed_file=None,
            semaphore_url="http://fakes/semaphore",
            semaphore_token="test",
            openobserve_url="http://fakes/openobserve",
            openobserve_user=None,
            openobserve_password=None,
            run_poll_seconds=0,
        )
        sem_http = TestClient(fakes.app, base_url="http://fakes/semaphore")
        oo_http = TestClient(fakes.app, base_url="http://fakes/openobserve")
        p = Platform(
            settings=settings,
            db=Database(database_url, max_size=3),
            tenants=Tenants.load(settings.tenants_file),
            catalog=Catalog.load(settings.catalog_file),
            semaphore=Semaphore(settings.semaphore_url, "test", client=sem_http),
            telemetry=OpenObserve(settings.openobserve_url, None, None, client=oo_http),
            anthropic=anthropic,
        )
        made.append(p)
        return p

    yield _make
    for p in made:
        p.db.close()


SQL_ALERT = {
    "alert_id": "sql-log-used-high",
    "org": "lab",
    "url": "http://openobserve.local/web/alerts?org=lab",
    "rows": [{"tenant_id": "lab", "host_name": "lab-sql01", "sqlserver_database_name": "FinanceERP", "used_pct": 96.4}],
}


def drain(platform: Platform, limit: int = 50) -> list[str]:
    """Run queued jobs until none are ready, the way the worker would. Returns the job kinds run."""
    from estate_ops import jobs

    ran = []
    for _ in range(limit):
        job = jobs.claim(platform.db)
        if job is None:
            return ran
        try:
            platform.handle(job)
        except Exception as exc:
            jobs.fail(platform.db, job, str(exc))
            raise
        ran.append(job["kind"])
    raise AssertionError(f"jobs did not settle after {limit} runs: {ran[-10:]}")
