"""NATS JetStream: the queue between alert intake and triage, and the stream of case events.

Two streams, named from the configured prefix (default "estate"):
- <PREFIX>_INTAKE, subjects <prefix>.intake.<tenant>: one message per accepted webhook, published by the API
  with Nats-Msg-Id = its intake id, so a retried publish is stored once. The worker's durable "triage"
  consumer takes them in order, so an alert storm queues here instead of hitting Postgres from the API.
- <PREFIX>_EVENTS, subjects <prefix>.events.<tenant>.<kind>: every ledger entry, relayed by the worker (relay.py)
  for whatever wants to react to cases without reading the database: Teams notices, a live console, later agents.
"""

from __future__ import annotations

import json
import re
from typing import Any

import nats
from nats.js.api import ConsumerConfig, StreamConfig
from nats.js.errors import NotFoundError

DAY = 24 * 3600


class Bus:
    def __init__(self, nc: nats.NATS, prefix: str):
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", prefix):
            raise ValueError("NATS prefix must be lower case letters, digits, - or _")
        self.nc, self.js, self.prefix = nc, nc.jetstream(), prefix
        stem = prefix.upper().replace("-", "_")
        self.intake_stream, self.events_stream = f"{stem}_INTAKE", f"{stem}_EVENTS"

    @classmethod
    async def connect(cls, url: str, prefix: str) -> Bus:
        return cls(await nats.connect(url, name=f"estate-ops-{prefix}", max_reconnect_attempts=-1), prefix)

    async def close(self) -> None:
        await self.nc.close()  # publishes are acknowledged, so nothing is left to flush

    async def ensure_streams(self) -> None:
        for config in (
            StreamConfig(name=self.intake_stream, subjects=[f"{self.prefix}.intake.>"], max_age=7 * DAY, duplicate_window=600),
            StreamConfig(
                name=self.events_stream,
                subjects=[f"{self.prefix}.events.>"],
                max_age=30 * DAY,
                max_bytes=1 << 30,
                duplicate_window=600,
            ),
        ):
            try:
                await self.js.stream_info(config.name)
            except NotFoundError:
                await self.js.add_stream(config)

    async def delete_streams(self) -> None:
        for name in (self.intake_stream, self.events_stream):
            try:
                await self.js.delete_stream(name)
            except NotFoundError:
                pass

    async def publish_intake(self, tenant_id: str, intake_id: str, payload: dict[str, Any]) -> bool:
        """Queue one webhook. Returns False when this intake id was already stored."""
        body = json.dumps({"intake_id": intake_id, "tenant_id": tenant_id, "payload": payload}, default=str).encode()
        ack = await self.js.publish(f"{self.prefix}.intake.{tenant_id}", body, headers={"Nats-Msg-Id": intake_id})
        return not ack.duplicate

    async def intake_subscription(self):
        return await self.js.pull_subscribe(
            f"{self.prefix}.intake.>",
            durable="triage",
            stream=self.intake_stream,
            config=ConsumerConfig(ack_wait=120, max_deliver=10),
        )

    async def publish_event(self, entry: dict[str, Any]) -> None:
        subject = f"{self.prefix}.events.{entry['tenant_id']}.{entry['kind']}"
        await self.js.publish(subject, json.dumps(entry, default=str).encode(), headers={"Nats-Msg-Id": f"ledger-{entry['id']}"})
