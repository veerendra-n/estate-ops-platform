"""Tests run against real Postgres, Neo4j, NATS and Temporal servers; row-level security, the ledger trigger,
graph isolation, redelivery and the workflow are part of what's tested.

- TEST_DATABASE_URL: a Postgres where the user may create databases. Each test gets a fresh database cloned
  from a migrated template.
- TEST_NEO4J_URL / TEST_NEO4J_PASSWORD: a Neo4j seeded once per session (assets are keyed by tenant).
- TEST_NATS_URL and TEST_TEMPORAL_ADDRESS (a `temporal server start-dev`): each test uses its own NATS
  prefix and task queue, and runs its own worker in a thread.
Semaphore and OpenObserve are the in-process fakes from dev/.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
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
from estate_ops import worker  # noqa: E402
from estate_ops.api import create_app  # noqa: E402
from estate_ops.bus import Bus  # noqa: E402
from estate_ops.catalog import Catalog  # noqa: E402
from estate_ops.config import Settings  # noqa: E402
from estate_ops.db import Database  # noqa: E402
from estate_ops.estate import EstateGraph  # noqa: E402
from estate_ops.semaphore import Semaphore  # noqa: E402
from estate_ops.service import Platform  # noqa: E402
from estate_ops.telemetry import OpenObserve  # noqa: E402
from estate_ops.tenancy import Tenants  # noqa: E402

BASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@localhost:5432/postgres")
NEO4J_URL = os.environ.get("TEST_NEO4J_URL", "bolt://localhost:7687")
NEO4J_PASSWORD = os.environ.get("TEST_NEO4J_PASSWORD", "test-password")
NATS_URL = os.environ.get("TEST_NATS_URL", "nats://localhost:4222")
TEMPORAL_ADDRESS = os.environ.get("TEST_TEMPORAL_ADDRESS", "localhost:7233")
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
    yield TEMPLATE
    with psycopg.connect(BASE_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEMPLATE} WITH (FORCE)")


@pytest.fixture(scope="session")
def graph():
    g = EstateGraph(NEO4J_URL, "neo4j", NEO4J_PASSWORD)
    for attempt in range(60):  # Neo4j opens its port a little before it accepts logins
        try:
            g.driver.verify_connectivity()
            break
        except Exception:
            if attempt == 59:
                raise
            time.sleep(2)
    g.ensure_schema()
    g.load_seed(ROOT / "config/seed/lab.yml")
    # A second customer with servers of the same kind, to prove isolation.
    g.upsert(
        "contoso",
        "test",
        [
            {"id": "host:cto-sql01", "kind": "server", "name": "cto-sql01"},
            {"id": "host:cto-prt01", "kind": "server", "name": "cto-prt01"},
        ],
        [],
    )
    yield g
    g.close()


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
    fakes.reset()
    fakes._tasks.clear()
    return TestClient(fakes.app)


@pytest.fixture
def make_platform(database_url, graph, fakes_client, monkeypatch):
    monkeypatch.setenv("LAB_INTAKE_TOKEN", "lab-test-token")
    monkeypatch.setenv("CONTOSO_INTAKE_TOKEN", "contoso-test-token")
    monkeypatch.setenv("CONSOLE_TOKEN", "console-test-token")
    monkeypatch.setenv("LAB_TEAMS_WEBHOOK", "http://fakes/teams/lab")
    made = []

    def _make(anthropic=None, tenants_file=None, **settings_overrides) -> Platform:
        run = uuid.uuid4().hex[:8]
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
            nats_url=NATS_URL,
            nats_prefix=f"t{run}",
            temporal_address=TEMPORAL_ADDRESS,
            task_queue=f"test-{run}",
            run_poll_seconds=0.2,
            **settings_overrides,
        )
        sem_http = TestClient(fakes.app, base_url="http://fakes/semaphore")
        oo_http = TestClient(fakes.app, base_url="http://fakes/openobserve")
        p = Platform(
            settings=settings,
            db=Database(database_url, max_size=6),
            graph=graph,
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


class Stack:
    """A platform with its worker running in a background thread and an API client, as in production."""

    def __init__(self, platform: Platform, integrations: list | None = None):
        self.platform = platform
        self.integrations = integrations or []
        self._stop: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(30):
            raise RuntimeError(f"worker did not start: {self._error!r}")
        self._api_cm = TestClient(create_app(platform))
        self.api = self._api_cm.__enter__()

    def _run(self) -> None:
        async def main():
            self._loop = asyncio.get_running_loop()
            self._stop = asyncio.Event()
            ready = asyncio.Event()
            task = asyncio.create_task(worker.run(self.platform, self._stop, ready, self.integrations))
            await ready.wait()
            self._ready.set()
            await task

        try:
            asyncio.run(main())
        except BaseException as exc:
            self._error = exc
            self._ready.set()

    def close(self) -> None:
        self._api_cm.__exit__(None, None, None)
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(30)
        if self._thread.is_alive():
            raise RuntimeError("worker did not stop")

        async def drop():
            bus = await Bus.connect(self.platform.settings.nats_url, self.platform.settings.nats_prefix)
            await bus.delete_streams()
            await bus.close()

        asyncio.run(drop())


@pytest.fixture
def make_stack(make_platform):
    stacks = []

    def _make(integrations=None, **kwargs) -> Stack:
        platform = make_platform(**kwargs)
        stack = Stack(platform, [make(platform) for make in integrations or []])
        stacks.append(stack)
        return stack

    yield _make
    for stack in stacks:
        stack.close()


SQL_ALERT = {
    "alert_id": "sql-log-used-high",
    "org": "lab",
    "url": "http://openobserve.local/web/alerts?org=lab",
    "rows": [{"tenant_id": "lab", "host_name": "lab-sql01", "sqlserver_database_name": "FinanceERP", "used_pct": 96.4}],
}


def wait_for(check, timeout: float = 30, what: str = "condition"):
    """Poll check() until it returns something truthy, and return that."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")
