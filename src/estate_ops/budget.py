"""AI spend per tenant: metered on every model call and checked before the next one."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg

from .config import Settings
from .tenancy import Tenant

URGENT = {"P1", "P2"}


def cost_usd(settings: Settings, model: str, usage: Any) -> Decimal:
    p_in, p_out, p_cache_read, p_cache_write = settings.prices.get(model, settings.prices["claude-opus-5-5"])
    tokens = (
        getattr(usage, "input_tokens", 0) * p_in
        + getattr(usage, "output_tokens", 0) * p_out
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * p_cache_read
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * p_cache_write
    )
    return Decimal(str(round(tokens / 1_000_000, 6)))


def record(
    conn: psycopg.Connection, settings: Settings, tenant_id: str, case_id: str | None, agent: str, model: str, usage: Any
) -> Decimal:
    cost = cost_usd(settings, model, usage)
    conn.execute(
        "INSERT INTO ai_usage (tenant_id, case_id, agent, model, input_tokens, output_tokens, "
        "cache_read_tokens, cache_write_tokens, cost_usd) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            tenant_id,
            case_id,
            agent,
            model,
            getattr(usage, "input_tokens", 0),
            getattr(usage, "output_tokens", 0),
            getattr(usage, "cache_read_input_tokens", 0) or 0,
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cost,
        ),
    )
    return cost


def month_spend(conn: psycopg.Connection, tenant_id: str) -> Decimal:
    row = conn.execute(
        "SELECT coalesce(sum(cost_usd), 0) AS spend FROM ai_usage WHERE tenant_id = %s AND at >= date_trunc('month', now())",
        (tenant_id,),
    ).fetchone()
    return Decimal(row["spend"])


def allows_model(conn: psycopg.Connection, tenant: Tenant, severity: str) -> tuple[bool, str]:
    """Urgent incidents are never blocked; everything else stops using the model above the budget."""
    spend = month_spend(conn, tenant.id)
    budget = Decimal(str(tenant.ai_budget_usd_month))
    if spend < budget:
        return True, f"${spend:.2f} of ${budget:.0f} used this month"
    if severity in URGENT:
        return True, f"over budget (${spend:.2f} of ${budget:.0f}) but {severity} is never blocked"
    return False, f"monthly AI budget reached (${spend:.2f} of ${budget:.0f})"
