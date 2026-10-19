"""The worker: Temporal activities and the case workflow, the NATS triage consumer, the ledger relay,
event integrations (Teams), alert polling (Veeam, Microsoft 365) and discovery into the estate graph
(vCenter, Veeam, Intune, Microsoft 365, the edge node's network view, declared services).

One process runs all of them. Several workers can run side by side: Temporal hands each activity to one of
them, the durable NATS consumer hands each intake message to one of them, and the relay cursor is updated
under a row lock.
"""

from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from . import integrations, sources, triage
from .bus import Bus
from .service import Platform, WorkflowError
from .workflows import CaseInput, CaseWorkflow, workflow_id

log = logging.getLogger("estate_ops.worker")


def _activity(name: str, fn):
    """A sync Platform step as an activity. Refusals become non-retryable failures named after their type."""

    @activity.defn(name=name)
    def run(*args: Any) -> Any:
        try:
            return fn(*args)
        except (WorkflowError, LookupError) as exc:
            raise ApplicationError(str(exc), type=type(exc).__name__, non_retryable=True) from exc

    return run


def activities(platform: Platform) -> list:
    return [
        _activity("investigate", platform.investigate),
        _activity("run_skill", platform.run_skill),
        _activity("decide", platform.decide),
        _activity("expire_approval", platform.expire_approval),
        _activity("escalate", platform.escalate),
    ]


async def temporal_client(platform: Platform) -> Client:
    s = platform.settings
    return await Client.connect(s.temporal_address, namespace=s.temporal_namespace)


async def start_case(client: Client, platform: Platform, tenant_id: str, case_id: str) -> None:
    s = platform.settings
    await client.start_workflow(
        CaseWorkflow.run,
        CaseInput(tenant_id, case_id, s.settle_seconds, s.approval_timeout_hours * 3600),
        id=workflow_id(case_id),
        task_queue=s.task_queue,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )


def _ingest(platform: Platform, message: dict[str, Any]) -> list[triage.Triaged]:
    tenant = platform.tenants.get(message["tenant_id"])  # KeyError for a tenant removed since it was queued
    s = platform.settings
    with platform.db.scope(tenant.id) as conn:
        return triage.ingest(
            conn, tenant, message["payload"], message["intake_id"], platform.graph, s.correlate_hops, s.correlate_minutes
        )


async def run_triage(platform: Platform, bus: Bus, client: Client, stop: asyncio.Event) -> None:
    sub = await bus.intake_subscription()
    while not stop.is_set():
        try:
            msgs = await sub.fetch(10, timeout=1)
        except TimeoutError:
            continue
        for msg in msgs:
            try:
                message = json.loads(msg.data)
                results = await asyncio.to_thread(_ingest, platform, message)
            except (triage.IntakeError, KeyError, ValueError) as exc:
                log.warning("intake message dropped: %s", exc)  # the API checked it, so this is not worth retrying
                await msg.term()
                continue
            except Exception:
                log.exception("triage failed; NATS will redeliver")
                await msg.nak(delay=5)
                continue
            if message["payload"].get("source") == "metrics":
                detail = {"alerts_total": 1, "last_alert": message["payload"].get("alert_id")}
                await asyncio.to_thread(sources.record, platform, message["tenant_id"], "metrics-alerts", "alerts", True, detail)
            try:
                # A redelivery after a crash here finds the signals already stored and still-open cases.
                for r in results:
                    if r.open:
                        await start_case(client, platform, message["tenant_id"], r.case_id)
            except Exception:
                log.exception("starting a case workflow failed; NATS will redeliver")
                await msg.nak(delay=5)
                continue
            await msg.ack()


def _relay_batch(platform: Platform, tenant_id: str, limit: int = 200) -> tuple[int, list[dict[str, Any]]]:
    with platform.db.scope(tenant_id) as conn:
        name = f"ledger:{tenant_id}"
        conn.execute("INSERT INTO relay_cursor (name, last_id) VALUES (%s, 0) ON CONFLICT DO NOTHING", (name,))
        last = conn.execute("SELECT last_id FROM relay_cursor WHERE name = %s", (name,)).fetchone()["last_id"]
        rows = conn.execute(
            "SELECT id, at, tenant_id, case_id, actor, kind, body, hash FROM ledger "
            "WHERE tenant_id = %s AND id > %s ORDER BY id LIMIT %s",
            (tenant_id, last, limit),
        ).fetchall()
    return last, rows


def _relay_commit(platform: Platform, tenant_id: str, last_id: int) -> None:
    with platform.db.scope(tenant_id) as conn:
        conn.execute(
            "UPDATE relay_cursor SET last_id = greatest(last_id, %s), updated_at = now() WHERE name = %s",
            (last_id, f"ledger:{tenant_id}"),
        )


async def run_relay(platform: Platform, bus: Bus, stop: asyncio.Event, every: float = 1.0) -> None:
    """Publish new ledger entries as events. A tenant's ledger is written under one lock, so its ids commit
    in order and a per-tenant cursor misses nothing; a crash re-publishes, and NATS drops the repeats."""
    while not stop.is_set():
        for tenant_id in platform.tenants.ids():
            try:
                _, rows = await asyncio.to_thread(_relay_batch, platform, tenant_id)
                for row in rows:
                    await bus.publish_event(row)
                if rows:
                    await asyncio.to_thread(_relay_commit, platform, tenant_id, rows[-1]["id"])
            except Exception:
                log.exception("ledger relay for %s failed; retrying", tenant_id)
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
        except TimeoutError:
            pass


def configured_integrations(platform: Platform) -> list:
    from .integrations.teams import Teams

    return [Teams(platform)] if any(t.teams_webhook() for t in platform.tenants) else []


async def _every(minutes: float, stop: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=minutes * 60)
    except TimeoutError:
        pass


async def run_discovery(platform: Platform, stop: asyncio.Event) -> None:
    while not stop.is_set():
        log.info("discovery: %s", await asyncio.to_thread(sources.discover_all, platform))
        await _every(platform.settings.discovery_interval_minutes, stop)


async def run_polling(platform: Platform, bus: Bus, stop: asyncio.Event) -> None:
    """Queue what the polled alert sources report, like webhooks. Intake ids are stable per event, so
    NATS drops a repeat inside its window and triage folds any later one into nothing."""
    while not stop.is_set():
        for tenant in platform.tenants:
            for poller in sources.POLLERS:
                if poller.connector not in (tenant.connectors or {}):
                    continue
                for intake_id, payload in await asyncio.to_thread(sources.poll, platform, tenant, poller):
                    try:
                        await bus.publish_intake(tenant.id, intake_id, payload)
                    except Exception:
                        log.exception("queueing %s from %s failed; the next poll sends it again", intake_id, poller.name)
        await _every(platform.settings.poll_interval_minutes, stop)


async def run(
    platform: Platform,
    stop: asyncio.Event,
    ready: asyncio.Event | None = None,
    with_integrations: list | None = None,
    with_feeds: bool = True,
) -> None:
    s = platform.settings
    client = await temporal_client(platform)
    bus = await Bus.connect(s.nats_url, s.nats_prefix)
    await bus.ensure_streams()
    with ThreadPoolExecutor(max_workers=8) as pool:
        worker = Worker(
            client,
            task_queue=s.task_queue,
            workflows=[CaseWorkflow],
            activities=activities(platform),
            activity_executor=pool,
        )
        async with worker:
            tasks = [
                asyncio.create_task(run_triage(platform, bus, client, stop)),
                asyncio.create_task(run_relay(platform, bus, stop)),
            ]
            chosen = configured_integrations(platform) if with_integrations is None else with_integrations
            for integration in chosen:
                tasks.append(asyncio.create_task(integrations.run(platform, bus, integration, stop)))
                log.info("integration %s on", integration.name)
            if with_feeds:
                tasks.append(asyncio.create_task(run_discovery(platform, stop)))
                tasks.append(asyncio.create_task(run_polling(platform, bus, stop)))
            log.info(
                "worker started (queue %s, model %s)",
                s.task_queue,
                s.diagnosis_model if platform.anthropic else "none, deterministic only",
            )
            if ready:
                ready.set()
            await stop.wait()
            await asyncio.gather(*tasks)
    await bus.close()
