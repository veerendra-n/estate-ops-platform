"""Tests run against real Postgres, Neo4j, NATS, Temporal and OPA servers; row-level security, the ledger
trigger, graph isolation, correlation, redelivery, policy and the workflow are part of what's tested. The
customers' systems are the estate simulator, in process.

- TEST_DATABASE_URL: a Postgres where the user may create databases. Each test gets a fresh database cloned
  from a migrated template.
- TEST_NEO4J_URL / TEST_NEO4J_PASSWORD: a Neo4j, filled once per session by discovery from the simulator.
- TEST_NATS_URL, TEST_TEMPORAL_ADDRESS (a `temporal server start-dev`): each test uses its own NATS prefix
  and task queue, and runs its own worker in a thread.
- TEST_OPA_URL: OPA serving policy/autonomy.rego.
Metrics come from the simulator's /metrics instead of VictoriaMetrics (SimMetrics below).
"""

from __future__ import annotations

import asyncio
import os
import re
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

import estate_sim.app as sim  # noqa: E402
from estate_ops import db as dbmod  # noqa: E402
from estate_ops import sources, worker  # noqa: E402
from estate_ops.api import create_app  # noqa: E402
from estate_ops.bus import Bus  # noqa: E402
from estate_ops.config import Settings  # noqa: E402
from estate_ops.connectors.telemetry import METRICS  # noqa: E402
from estate_ops.db import Database  # noqa: E402
from estate_ops.estate import EstateGraph  # noqa: E402
from estate_ops.policy import Policy  # noqa: E402
from estate_ops.service import Platform  # noqa: E402
from estate_ops.skills import Skills  # noqa: E402
from estate_ops.tenancy import Tenants  # noqa: E402

BASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@localhost:5432/postgres")
NEO4J_URL = os.environ.get("TEST_NEO4J_URL", "bolt://localhost:7687")
NEO4J_PASSWORD = os.environ.get("TEST_NEO4J_PASSWORD", "test-password")
NATS_URL = os.environ.get("TEST_NATS_URL", "nats://localhost:4222")
TEMPORAL_ADDRESS = os.environ.get("TEST_TEMPORAL_ADDRESS", "localhost:7233")
OPA_URL = os.environ.get("TEST_OPA_URL", "http://localhost:8181")
TEMPLATE = "estate_ops_test_template"
TOKENS = {"CONSOLE_TOKEN": "console-test-token", "ALERTS_INTAKE_TOKEN": "alerts-test-token"}
for _t in ("CONTOSO", "FABRIKAM"):
    TOKENS[f"{_t}_INTAKE_TOKEN"] = f"{_t.lower()}-test-token"
    for _v in ("VCENTER_USER", "VCENTER_PASSWORD", "VEEAM_USER", "VEEAM_PASSWORD", "GRAPH_SECRET", "FLEET_TOKEN", "EDGE_TOKEN"):
        TOKENS[f"{_t}_{_v}"] = "sim"
os.environ.update(TOKENS)


def _url(dbname: str) -> str:
    return psycopg.conninfo.make_conninfo(BASE_URL, dbname=dbname)


class SimMetrics:
    """Answers named metrics from the simulator's /metrics, as VictoriaMetrics would from its scrape."""

    def __init__(self, client: TestClient, tenant: str):
        self.client, self.tenant = client, tenant

    def _values(self, metric: str, asset: str) -> list[float]:
        template = METRICS[metric][0]
        name = re.search(r"\(([a-z_]+)\{", template).group(1)
        label = re.search(r'\{([a-z_]+)="\$asset"', template).group(1)
        return [
            float(line.rsplit(" ", 1)[1])
            for line in self.client.get("/metrics").text.splitlines()
            if line.startswith(name + "{") and f'tenant_id="{self.tenant}"' in line and f'{label}="{asset}"' in line
        ]

    def latest(self, metric: str, asset: str):
        values = self._values(metric, asset)
        if not values:
            return None
        return min(values) if METRICS[metric][0].startswith("min") else max(values)

    def series(self, metric: str, asset: str, minutes: int = 60):
        v = self.latest(metric, asset)
        return {"metric": metric, "asset": asset, "series": [{"last": v, "max": v, "min": v, "points": [v]}]}


class SimLogs:
    def search(self, asset: str, phrase: str, minutes: int = 60, limit: int = 20):
        lines = [m for e in sim.ESTATES.values() for h, _, m in sim._log_lines(e) if h == asset and phrase.lower() in m.lower()]
        return {"asset": asset, "lines": lines[:limit]}


class TestPlatform(Platform):
    """The platform with connectors bound to the in-process simulator."""

    __test__ = False

    def connectors(self, tenant):
        conns = super().connectors(tenant)
        conns.metrics = lambda: SimMetrics(self.http, tenant.id)
        conns.logs = lambda: SimLogs()
        return conns


@pytest.fixture(scope="session")
def sim_client():
    sim.JOB_SECONDS = 0
    return TestClient(sim.app, base_url="http://sim:8095")


@pytest.fixture
def estate(sim_client):
    """A fresh simulated estate for each test."""
    sim_client.post("/_sim/reset")
    yield sim_client
    sim_client.post("/_sim/reset")


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


@pytest.fixture
def database_url(template_db):
    name = f"estate_ops_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(BASE_URL, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name} TEMPLATE {template_db}")
    yield _url(name)
    with psycopg.connect(BASE_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def settings_for(database_url: str, **overrides) -> Settings:
    run = uuid.uuid4().hex[:8]
    values = {
        "database_url": database_url,
        "tenants_file": ROOT / "config/tenants.yml",
        "skills_dir": ROOT / "skills",
        "declared_dir": ROOT / "config/estate",
        "opa_url": OPA_URL,
        "neo4j_url": NEO4J_URL,
        "neo4j_password": NEO4J_PASSWORD,
        "nats_url": NATS_URL,
        "nats_prefix": f"t{run}",
        "temporal_address": TEMPORAL_ADDRESS,
        "task_queue": f"test-{run}",
        "settle_seconds": 1.0,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(scope="session")
def graph(sim_client, template_db):
    """The estate graph, discovered from the simulator once (with a throwaway database for the status rows)."""
    g = EstateGraph(NEO4J_URL, "neo4j", NEO4J_PASSWORD)
    for attempt in range(60):  # Neo4j opens its port a little before it accepts logins
        try:
            g.driver.verify_connectivity()
            break
        except Exception:
            if attempt == 59:
                raise
            time.sleep(2)
    g.driver.execute_query("MATCH (a:Asset) DETACH DELETE a")
    g.ensure_schema()
    sim_client.post("/_sim/reset")
    name = f"estate_ops_test_graph_{uuid.uuid4().hex[:6]}"
    with psycopg.connect(BASE_URL, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name} TEMPLATE {template_db}")
    settings = settings_for(_url(name))
    p = _platform(settings, g, sim_client, None)
    result = sources.discover_all(p)
    p.db.close()
    with psycopg.connect(BASE_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    assert all("error" not in r for t in result.values() for r in t.values()), result
    yield g
    g.close()


def _platform(settings: Settings, graph, sim_client, anthropic, policy=None) -> TestPlatform:
    return TestPlatform(
        settings=settings,
        db=Database(settings.database_url, max_size=6),
        graph=graph,
        tenants=Tenants.load(settings.tenants_file),
        skills=Skills.load(settings.skills_dir, "test"),
        policy=policy or Policy(settings.opa_url),
        anthropic=anthropic,
        http=sim_client,
    )


@pytest.fixture
def make_platform(database_url, graph, estate):
    made = []

    def _make(anthropic=None, policy=None, **settings_overrides) -> TestPlatform:
        p = _platform(settings_for(database_url, **settings_overrides), graph, estate, anthropic, policy)
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
            task = asyncio.create_task(worker.run(self.platform, self._stop, ready, self.integrations, with_feeds=False))
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


def alert(name: str, tenant: str, labels: dict, severity: str = "P2", title: str | None = None, value: str = "", ended=False):
    """One alert as vmalert posts it to the Alertmanager API."""
    a = {
        "labels": {"alertname": name, "tenant_id": tenant, "severity": severity, **labels},
        "annotations": {"title": title or f"{name} {next(iter(labels.values()), '')}".strip(), "value": value},
        "startsAt": "2026-01-01T10:00:00Z",
        "generatorURL": "http://vmalert/vmui",
    }
    if ended:
        a["endsAt"] = "2026-01-01T10:05:00Z"
    return a


def post_alerts(api, alerts: list[dict], token: str = "alerts-test-token"):
    return api.post("/api/v1/intake/alertmanager/api/v2/alerts", json=alerts, headers={"Authorization": f"Bearer {token}"})


def wait_for(check, timeout: float = 30, what: str = "condition"):
    """Poll check() until it returns something truthy, and return that."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")
