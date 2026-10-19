"""What the cockpit reads: cases, the overview, the estate, skills and autonomy, integrations, AI cost,
and the case copilot. Every function takes the engineer's tenant scope and reads through it, so row-level
security and the graph's tenant match apply as everywhere else. Nothing here changes an estate.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from . import budget, ledger
from .agents import copilot
from .policy import build_input
from .service import Platform

FEED_KINDS = (
    "case_opened",
    "investigation",
    "proposal",
    "approval_requested",
    "approved",
    "rejected",
    "run_result",
    "verified",
    "escalated",
    "resolved",
)
CLOSED = ("resolved", "escalated", "merged")


def cases(platform: Platform, scope: list[str], status: str | None, source: str | None, q: str | None) -> list[dict[str, Any]]:
    with platform.db.scope(scope) as conn:
        return conn.execute(
            "SELECT c.id, c.tenant_id, t.name AS tenant_name, c.ref, c.type, c.title, c.severity, c.status, c.target, c.source, "
            "c.service, c.summary, c.opened_at, c.updated_at, c.responded_at, c.resolved_at, c.respond_due, c.resolve_due, "
            "(SELECT count(*) FROM signals s WHERE s.case_id = c.id) AS signals "
            "FROM cases c JOIN tenants t ON t.id = c.tenant_id "
            "WHERE (%(status)s::text IS NULL OR c.status = %(status)s "
            "       OR (%(status)s = 'active' AND c.status NOT IN ('resolved', 'escalated', 'merged'))) "
            "AND (%(source)s::text IS NULL OR c.source = %(source)s) "
            "AND (%(q)s::text IS NULL OR c.title ILIKE '%%' || %(q)s || '%%' OR c.ref ILIKE '%%' || %(q)s || '%%' "
            "     OR c.target ILIKE '%%' || %(q)s || '%%') "
            "ORDER BY c.updated_at DESC LIMIT 300",
            {"status": status, "source": source, "q": q or None},
        ).fetchall()


def case_detail(platform: Platform, scope: list[str], case_id: str) -> dict[str, Any] | None:
    with platform.db.scope(scope) as conn:
        case = conn.execute(
            "SELECT c.*, t.name AS tenant_name FROM cases c JOIN tenants t ON t.id = c.tenant_id WHERE c.id = %s",
            (case_id,),
        ).fetchone()
        if case is None:
            return None
        return {
            "case": case,
            "signals": conn.execute(
                "SELECT alert_id, source, target, asset_id, labels, received_at FROM signals WHERE case_id = %s ORDER BY id",
                (case_id,),
            ).fetchall(),
            "proposals": conn.execute("SELECT * FROM proposals WHERE case_id = %s ORDER BY created_at", (case_id,)).fetchall(),
            "timeline": ledger.for_case(conn, case_id),
            "external_refs": conn.execute(
                "SELECT system, ref, created_at FROM external_refs WHERE case_id = %s ORDER BY created_at", (case_id,)
            ).fetchall(),
            "ai_cost_usd": float(
                conn.execute("SELECT coalesce(sum(cost_usd), 0) AS c FROM ai_usage WHERE case_id = %s", (case_id,)).fetchone()[
                    "c"
                ]
            ),
            "blast_radius": _blast_radius(platform, case["tenant_id"], list(case["assets"] or [])),
        }


def _blast_radius(platform: Platform, tenant_id: str, assets: list[str]) -> dict[str, Any] | None:
    if not assets:
        return None
    try:
        return platform.graph.blast_radius(tenant_id, assets)
    except Exception:  # the case still opens while Neo4j is down
        return None


def overview(platform: Platform, scope: list[str]) -> dict[str, Any]:
    """Per customer: what is open, what waits on people, how the last 7 days went, AI spend, estate size."""
    per_tenant = []
    with platform.db.scope(scope) as conn:
        rows = {
            r["tenant_id"]: r
            for r in conn.execute(
                "SELECT tenant_id, "
                "count(*) FILTER (WHERE status NOT IN ('resolved', 'escalated', 'merged')) AS open, "
                "count(*) FILTER (WHERE status = 'awaiting_approval') AS awaiting_approval, "
                "count(*) FILTER (WHERE status = 'escalated' AND updated_at > now() - interval '7 days') AS escalated_7d, "
                "count(*) FILTER (WHERE status = 'resolved' AND resolved_at > now() - interval '7 days') AS resolved_7d, "
                "count(*) FILTER (WHERE status = 'resolved' AND resolved_at > now() - interval '7 days' AND NOT EXISTS "
                "  (SELECT 1 FROM ledger l WHERE l.case_id = cases.id AND l.kind IN ('approved', 'rejected') "
                "   AND l.actor LIKE 'engineer:%%')) AS resolved_by_agents_7d, "
                "count(*) FILTER (WHERE opened_at > now() - interval '24 hours') AS opened_24h, "
                "percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM resolved_at - opened_at)) "
                "  FILTER (WHERE status = 'resolved' AND resolved_at > now() - interval '7 days') AS median_resolve_seconds, "
                # At risk: open and past three quarters of its resolve clock, or not yet responded past three quarters.
                "count(*) FILTER (WHERE status NOT IN ('resolved', 'escalated', 'merged') AND ("
                "  now() > opened_at + (resolve_due - opened_at) * 0.75 OR "
                "  (responded_at IS NULL AND now() > opened_at + (respond_due - opened_at) * 0.75))) AS sla_at_risk, "
                "count(*) FILTER (WHERE opened_at > now() - interval '30 days' AND ("
                "  coalesce(responded_at, now()) > respond_due OR coalesce(resolved_at, now()) > resolve_due)) "
                "  AS sla_breached_30d, "
                "count(*) FILTER (WHERE opened_at > now() - interval '30 days') AS cases_30d "
                "FROM cases GROUP BY tenant_id"
            ).fetchall()
        }
        spend = {
            r["tenant_id"]: r["spend"]
            for r in conn.execute(
                "SELECT tenant_id, coalesce(sum(cost_usd), 0) AS spend FROM ai_usage "
                "WHERE at >= date_trunc('month', now()) GROUP BY tenant_id"
            ).fetchall()
        }
        feed = conn.execute(
            "SELECT l.id, l.at, l.tenant_id, l.actor, l.kind, l.body, c.id AS case_id, c.ref, c.title "
            "FROM ledger l JOIN cases c ON c.id = l.case_id WHERE l.kind = ANY(%s) ORDER BY l.id DESC LIMIT 40",
            (list(FEED_KINDS),),
        ).fetchall()
    for t in platform.tenants:
        if t.id not in scope:
            continue
        r = rows.get(t.id) or {}
        try:
            estate_total = platform.graph.stats(t.id)["total"]
        except Exception:  # the overview still shows cases while Neo4j is down
            estate_total = None
        median = r.get("median_resolve_seconds")
        cases_30d = r.get("cases_30d", 0) or 0
        per_tenant.append(
            {
                "id": t.id,
                "name": t.name,
                "industry": t.industry,
                "autonomy_default": t.autonomy.get("default", "approve_to_run"),
                "sla_at_risk": r.get("sla_at_risk", 0),
                "sla_breached_30d": r.get("sla_breached_30d", 0),
                "sla_met_pct_30d": round(100 * (cases_30d - (r.get("sla_breached_30d") or 0)) / cases_30d, 1)
                if cases_30d
                else None,
                "open": r.get("open", 0),
                "awaiting_approval": r.get("awaiting_approval", 0),
                "escalated_7d": r.get("escalated_7d", 0),
                "resolved_7d": r.get("resolved_7d", 0),
                "resolved_by_agents_7d": r.get("resolved_by_agents_7d", 0),
                "opened_24h": r.get("opened_24h", 0),
                "median_resolve_minutes_7d": round(median / 60, 1) if median is not None else None,
                "ai_spend_month_usd": float(spend.get(t.id, Decimal(0))),
                "ai_budget_usd_month": t.ai_budget_usd_month,
                "assets": estate_total,
            }
        )
    keys = ("open", "awaiting_approval", "resolved_7d", "resolved_by_agents_7d", "opened_24h", "sla_at_risk")
    totals = {k: sum((x[k] or 0) for x in per_tenant) for k in keys}
    return {"tenants": per_tenant, "totals": totals, "feed": feed}


def estate(platform: Platform, tenant_id: str, q: str, kind: str | None) -> dict[str, Any]:
    with platform.db.scope(tenant_id) as conn:
        changes = conn.execute(
            "SELECT id, at, source, asset_id, asset_name, change, before, after FROM graph_changes "
            "WHERE tenant_id = %s ORDER BY at DESC LIMIT 100",
            (tenant_id,),
        ).fetchall()
    return {
        "stats": platform.graph.stats(tenant_id),
        "assets": platform.graph.assets(tenant_id, q, kind, 500),
        "changes": changes,
    }


def asset_detail(platform: Platform, tenant_id: str, asset_id: str, hops: int) -> dict[str, Any] | None:
    asset = platform.graph.asset(tenant_id, asset_id)
    if asset is None:
        return None
    with platform.db.scope(tenant_id) as conn:
        related = conn.execute(
            "SELECT id, ref, title, severity, status, opened_at FROM cases WHERE %s = ANY(assets) "
            "ORDER BY opened_at DESC LIMIT 20",
            (asset_id,),
        ).fetchall()
    return {"asset": asset, "graph": platform.graph.neighbourhood(tenant_id, asset_id, hops), "cases": related}


def skills(platform: Platform, scope: list[str]) -> list[dict[str, Any]]:
    """The library with, per customer, the autonomy the policy allows at no blast radius, and how each skill has done."""
    with platform.db.scope(scope) as conn:
        stats = conn.execute(
            "SELECT tenant_id, skill_id, "
            "count(*) AS proposals, "
            "count(*) FILTER (WHERE status = 'done' AND outcome = 'fixed') AS fixed, "
            "count(*) FILTER (WHERE status = 'done' AND outcome = 'no_action') AS no_action, "
            "count(*) FILTER (WHERE status = 'failed') AS failed, "
            "count(*) FILTER (WHERE status = 'rejected' AND decided_by <> 'timeout') AS rejected, "
            "count(*) FILTER (WHERE status = 'failed' AND created_at > now() - interval '30 days') AS failures_30d, "
            "max(created_at) AS last_proposed "
            "FROM proposals GROUP BY tenant_id, skill_id"
        ).fetchall()
    by_key = {(s["tenant_id"], s["skill_id"]): s for s in stats}
    out = []
    for skill in platform.skills:
        per_tenant = []
        for t in platform.tenants:
            if t.id not in scope:
                continue
            s = by_key.get((t.id, skill.id)) or {}
            radius = {"dependents": 0, "services": [], "critical": False}
            history = {"live_runs_30d": 0, "failures_30d": s.get("failures_30d", 0) or 0}
            decision = platform.policy.decide(build_input(t, skill, ["-"], radius, history))
            live = (s.get("fixed", 0) or 0) + (s.get("failed", 0) or 0)
            per_tenant.append(
                {
                    "tenant_id": t.id,
                    "tenant_name": t.name,
                    "mode": decision.mode,
                    "why": decision.reason,
                    "proposals": s.get("proposals", 0),
                    "fixed": s.get("fixed", 0),
                    "no_action": s.get("no_action", 0),
                    "failed": s.get("failed", 0),
                    "rejected": s.get("rejected", 0),
                    "success_rate": round(100 * s.get("fixed", 0) / live, 1) if live else None,
                    "last_proposed": s.get("last_proposed"),
                }
            )
        out.append(skill.meta() | {"tenants": per_tenant})
    return out


def integrations(platform: Platform, scope: list[str], configured: dict[str, list[tuple[str, str]]]) -> list[dict[str, Any]]:
    """Each integration that is set up for a customer, with its last run. configured: tenant -> [(name, kind)]."""
    with platform.db.scope(scope) as conn:
        rows = {(r["tenant_id"], r["name"]): r for r in conn.execute("SELECT * FROM integration_status").fetchall()}
    out = []
    for t in platform.tenants:
        if t.id not in scope:
            continue
        for name, kind in configured.get(t.id, []):
            r = rows.pop((t.id, name), None)
            out.append(
                {
                    "tenant_id": t.id,
                    "tenant_name": t.name,
                    "name": name,
                    "kind": kind,
                    "state": "waiting" if r is None else ("ok" if r["ok"] else "failing"),
                    "last_run_at": r and r["last_run_at"],
                    "last_ok_at": r and r["last_ok_at"],
                    "last_error": r and r["last_error"],
                    "detail": (r and r["detail"]) or {},
                }
            )
    for (tenant_id, name), r in rows.items():  # ran before but is no longer configured
        out.append(
            {
                "tenant_id": tenant_id,
                "tenant_name": platform.tenants.get(tenant_id).name if tenant_id in platform.tenants.ids() else tenant_id,
                "name": name,
                "kind": r["kind"],
                "state": "off",
                "last_run_at": r["last_run_at"],
                "last_ok_at": r["last_ok_at"],
                "last_error": r["last_error"],
                "detail": r["detail"],
            }
        )
    return out


def cost(platform: Platform, scope: list[str], days: int) -> dict[str, Any]:
    days = max(1, min(int(days), 90))
    with platform.db.scope(scope) as conn:
        by_agent = conn.execute(
            "SELECT tenant_id, agent, model, count(*) AS calls, sum(input_tokens) AS input_tokens, "
            "sum(output_tokens) AS output_tokens, sum(cache_read_tokens) AS cache_read_tokens, sum(cost_usd) AS cost_usd "
            "FROM ai_usage WHERE at > now() - make_interval(days => %s) GROUP BY tenant_id, agent, model ORDER BY cost_usd DESC",
            (days,),
        ).fetchall()
        daily = conn.execute(
            "SELECT tenant_id, date_trunc('day', at)::date AS day, sum(cost_usd) AS cost_usd FROM ai_usage "
            "WHERE at > now() - make_interval(days => %s) GROUP BY 1, 2 ORDER BY 2",
            (days,),
        ).fetchall()
        per_case = conn.execute(
            "SELECT tenant_id, count(DISTINCT case_id) AS cases, coalesce(sum(cost_usd), 0) AS cost_usd FROM ai_usage "
            "WHERE at > now() - make_interval(days => %s) AND case_id IS NOT NULL GROUP BY tenant_id",
            (days,),
        ).fetchall()
        month = {
            r["tenant_id"]: r["spend"]
            for r in conn.execute(
                "SELECT tenant_id, sum(cost_usd) AS spend FROM ai_usage WHERE at >= date_trunc('month', now()) GROUP BY tenant_id"
            ).fetchall()
        }
    tenants = []
    for t in platform.tenants:
        if t.id not in scope:
            continue
        pc = next((r for r in per_case if r["tenant_id"] == t.id), None)
        tenants.append(
            {
                "id": t.id,
                "name": t.name,
                "diagnosis": t.diagnosis,
                "ai_spend_month_usd": float(month.get(t.id) or 0),
                "ai_budget_usd_month": t.ai_budget_usd_month,
                "cases_with_ai": pc["cases"] if pc else 0,
                "cost_per_case_usd": round(float(pc["cost_usd"]) / pc["cases"], 4) if pc and pc["cases"] else None,
            }
        )
    return {
        "days": days,
        "tenants": tenants,
        "by_agent": [r | {"cost_usd": float(r["cost_usd"])} for r in by_agent],
        "daily": [r | {"cost_usd": float(r["cost_usd"])} for r in daily],
    }


def ask_copilot(platform: Platform, scope: list[str], case_id: str, question: str, who: str) -> dict[str, Any] | None:
    detail = case_detail(platform, scope, case_id)
    if detail is None:
        return None
    case = detail["case"]
    tenant = platform.tenants.get(case["tenant_id"])
    history = []
    asked = None
    for e in detail["timeline"]:
        if e["kind"] == "copilot_question":
            asked = e["body"].get("question")
        elif e["kind"] == "copilot_answer" and asked:
            history.append({"question": asked, "answer": e["body"].get("answer", "")})
            asked = None
    with platform.db.scope(tenant.id) as conn:
        allowed, budget_note = budget.allows_model(conn, tenant, case["severity"])
        ledger.append(conn, tenant.id, case_id, f"engineer:{who}", "copilot_question", {"question": question})
    if platform.anthropic is None:
        answer = copilot.Answer("The copilot needs a Claude API key, and none is configured on this platform.", "unavailable")
    elif tenant.diagnosis == "deterministic":
        answer = copilot.Answer(f"AI is switched off for {tenant.name} (diagnosis: deterministic).", "unavailable")
    elif not allowed:
        answer = copilot.Answer(f"Not answered: {budget_note}.", "unavailable")
    else:
        conns = platform.connectors(tenant)
        tools = copilot.tools_for(platform.db, platform.graph, conns.metrics(), conns.logs(), tenant, case)
        try:
            answer = copilot.ask(
                platform.anthropic, platform.settings.copilot_model, copilot.case_record(detail), history, question, tools
            )
        except Exception as exc:
            answer = copilot.Answer(f"The copilot could not answer just now ({type(exc).__name__}).", "unavailable", tools.calls)
    with platform.db.scope(tenant.id) as conn:
        spent = sum(
            (budget.record(conn, platform.settings, tenant.id, case_id, "copilot", m, u) for m, u in answer.usage),
            start=Decimal(0),
        )
        body = {
            "answer": answer.text,
            "method": answer.method,
            "model": answer.model,
            "tools": [c["tool"] for c in answer.tools],
            "ai_cost_usd": float(spent),
        }
        entry = ledger.append(conn, tenant.id, case_id, "agent:copilot", "copilot_answer", body)
    return body | {"ledger_id": entry["id"]}
