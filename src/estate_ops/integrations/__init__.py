"""Integrations that react to case events: HaloPSA tickets and Microsoft Teams cards.

Each one is a durable consumer of the NATS events stream (one ledger entry per message), so it gets every
event in order, keeps up after an outage, and never slows a case down. Delivery is at least once:
an entry is marked delivered in Postgres after it succeeds, so a redelivered message is skipped, and a
crash between the two can repeat at most one note or card.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol

from ..bus import Bus
from ..tenancy import Tenant

log = logging.getLogger("estate_ops.integrations")


class Integration(Protocol):
    name: str

    def enabled(self, tenant: Tenant) -> bool: ...

    def deliver(self, tenant: Tenant, entry: dict[str, Any], case: dict[str, Any]) -> None: ...


def case_link(public_url: str, case_id: str) -> str:
    return f"{public_url.rstrip('/')}/#case={case_id}"


def _handle(platform, integration: Integration, entry: dict[str, Any]) -> str:
    tenant_id = entry["tenant_id"]
    try:
        tenant = platform.tenants.get(tenant_id)
    except KeyError:
        return "unknown tenant"
    if not entry.get("case_id") or not integration.enabled(tenant):
        return "skipped"
    with platform.db.scope(tenant_id) as conn:
        done = conn.execute(
            "SELECT 1 FROM deliveries WHERE tenant_id = %s AND system = %s AND ledger_id = %s",
            (tenant_id, integration.name, entry["id"]),
        ).fetchone()
        case = conn.execute(
            "SELECT id, ref, title, severity, status, target, summary FROM cases WHERE id = %s", (entry["case_id"],)
        ).fetchone()
    if done:
        return "duplicate"
    if case is None:
        return "no case"
    integration.deliver(tenant, entry, case)
    with platform.db.scope(tenant_id) as conn:
        conn.execute(
            "INSERT INTO deliveries (tenant_id, system, ledger_id) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (tenant_id, integration.name, entry["id"]),
        )
    return "delivered"


async def run(platform, bus: Bus, integration: Integration, stop: asyncio.Event) -> None:
    sub = await bus.events_subscription(f"int-{integration.name}")
    failures = 0
    while not stop.is_set():
        try:
            [msg] = await sub.fetch(1, timeout=1)
        except TimeoutError:
            continue
        entry = json.loads(msg.data)
        try:
            await asyncio.to_thread(_handle, platform, integration, entry)
        except Exception as exc:
            failures += 1
            delay = min(300, 2 ** min(failures, 8))
            log.warning("%s: entry %s failed (%s); retrying in %ss", integration.name, entry.get("id"), exc, delay)
            await msg.nak(delay=delay)
            continue
        failures = 0
        await msg.ack()
