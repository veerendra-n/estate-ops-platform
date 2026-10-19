"""Investigator agent: from a case's alerts to a root cause, evidence and the skill that answers it.

Two paths with the same output:
- deterministic: walk the estate graph. The alerting asset in the lowest layer (datastore below server below
  service) is the likely cause; the skill is the one whose target kind and trigger match an alert on it, with
  parameters from that alert's labels. No model call.
- llm: Claude reads the case and calls read-only tools (estate graph, named metrics, logs, recent changes,
  past cases) for a few turns, then answers in a fixed JSON schema. It may only pick a skill from the library,
  and the platform validates the pick again before anything is proposed.

The llm path falls back to the deterministic one when no model is configured, the customer is over its AI
budget, the model refuses or errs, or its answer fails validation. Investigation never acts on the estate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..connectors.telemetry import METRICS
from ..estate import LAYER_OF
from ..skills import Skills

SYSTEM = """You are the investigator agent of a managed IT operations service. Monitoring alerts have opened \
a case for one customer; related alerts have already been folded into it by the estate graph. Find the root \
cause and decide whether one skill from the library answers it.

Rules:
- Use only the case and what your tools return. Say what you checked; never invent readings.
- Symptoms sit above causes: a slow service is usually a slow server, which is usually its storage or host. \
Look at the lowest layer that alerted first, then confirm with a metric, a log or a recent change.
- You change nothing. A skill you pick is dry run first, policy decides how far it may go, and a verifier \
checks the result.
- Pick a skill only from the library below, only when the evidence supports it, and give its target as the \
asset's name (for example CTO-DS-CRIT-02, not datastore:CTO-DS-CRIT-02). Its target kind must match. \
Otherwise pick "none" and say what an engineer should look at.
- Parameters the alerts supply are fixed. Add optional parameters only when the evidence calls for it.
- Keep tool calls few. Write for an engineer on a phone: short sentences, the cause first.

Skill library:
{skills}
"""

TOOLS = [
    {
        "name": "estate_neighbourhood",
        "description": "Assets connected to an asset in the customer's estate graph (services, servers, hypervisor "
        "hosts, datastores, backup jobs and repositories) and how they relate. Asset ids look like "
        "server:CTO-SHOP-SQL01 or datastore:CTO-DS-CRIT-02.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"asset_id": {"type": "string"}, "hops": {"type": "integer", "enum": [1, 2, 3]}},
            "required": ["asset_id", "hops"],
            "additionalProperties": False,
        },
    },
    {
        "name": "metric",
        "description": "A named metric for one asset (by name, not id) over recent minutes, summarised: first, last, "
        "min, max and the last 12 points. Metrics: " + "; ".join(f"{k} ({v[1]})" for k, v in METRICS.items()),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "enum": list(METRICS)},
                "asset": {"type": "string"},
                "minutes": {"type": "integer", "enum": [15, 60, 240, 1440]},
            },
            "required": ["metric", "asset", "minutes"],
            "additionalProperties": False,
        },
    },
    {
        "name": "logs",
        "description": "Up to 20 recent log lines from one host (by name) containing a word or phrase.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "phrase": {"type": "string"},
                "minutes": {"type": "integer", "enum": [15, 60, 240, 1440]},
            },
            "required": ["host", "phrase", "minutes"],
            "additionalProperties": False,
        },
    },
    {
        "name": "recent_changes",
        "description": "What changed in the customer's estate in the last hours (VM moves, new versions, assets "
        "added or removed), for one asset id or, with an empty asset_id, for the whole estate.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"asset_id": {"type": "string"}, "hours": {"type": "integer", "enum": [1, 6, 24, 72]}},
            "required": ["asset_id", "hours"],
            "additionalProperties": False,
        },
    },
    {
        "name": "past_cases",
        "description": "Earlier cases of this customer that involved an asset id, with how each ended.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"asset_id": {"type": "string"}},
            "required": ["asset_id"],
            "additionalProperties": False,
        },
    },
]


def output_schema(skills: Skills) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "likely_cause": {"type": "string"},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "root_asset": {"type": "string"},
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"source": {"type": "string"}, "finding": {"type": "string"}},
                        "required": ["source", "finding"],
                        "additionalProperties": False,
                    },
                },
                "skill_id": {"type": "string", "enum": [*skills.ids(), "none"]},
                "target": {"type": "string"},
                "extra_params": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
                        "required": ["name", "value"],
                        "additionalProperties": False,
                    },
                },
                "engineer_should_check": {"type": "string"},
            },
            "required": [
                "summary",
                "likely_cause",
                "confidence",
                "root_asset",
                "evidence",
                "skill_id",
                "target",
                "extra_params",
                "engineer_should_check",
            ],
            "additionalProperties": False,
        },
    }


@dataclass
class Finding:
    method: str  # deterministic | llm
    summary: str
    likely_cause: str
    confidence: str
    evidence: list[dict[str, str]]
    root_asset: str | None
    skill_id: str | None
    target: str | None
    params: dict[str, Any]
    engineer_should_check: str = ""
    notes: list[str] = field(default_factory=list)
    usage: list[tuple[str, Any]] = field(default_factory=list)  # (model, usage) per model call

    def as_body(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "summary": self.summary,
            "likely_cause": self.likely_cause,
            "confidence": self.confidence,
            "root_asset": self.root_asset,
            "evidence": self.evidence,
            "skill_id": self.skill_id,
            "target": self.target,
            "params": self.params,
            "engineer_should_check": self.engineer_should_check,
            "notes": self.notes,
        }


@dataclass
class CaseContext:
    tenant_id: str
    case: dict[str, Any]
    signals: list[dict[str, Any]]  # alert_id, asset_id, target, labels, source
    kinds: dict[str, str]  # asset id -> kind, from the graph
    candidates: list[str]  # skills triggered by any alert in the case
    changes: list[dict[str, Any]]  # recent graph changes on the case's assets


def build_context(conn, skills: Skills, tenant, case: dict[str, Any], graph) -> CaseContext:
    signals = conn.execute(
        "SELECT alert_id, asset_id, target, labels, source FROM signals WHERE case_id = %s ORDER BY id", (case["id"],)
    ).fetchall()
    assets = sorted({s["asset_id"] for s in signals if s["asset_id"]})
    try:
        kinds = graph.kinds(tenant.id, assets)
    except Exception:
        kinds = {}
    for a in assets:  # the asset id carries its kind even when the graph doesn't know it yet
        kinds.setdefault(a, a.split(":", 1)[0])
    changes = conn.execute(
        "SELECT at, source, asset_id, asset_name, change, before, after FROM graph_changes "
        "WHERE tenant_id = %s AND at > now() - interval '24 hours' AND asset_id = ANY(%s) ORDER BY at DESC LIMIT 20",
        (tenant.id, assets),
    ).fetchall()
    candidates = sorted({sid for s in signals for sid in skills.for_alert(s["alert_id"])})
    return CaseContext(tenant.id, case, signals, kinds, candidates, changes)


def _alert_lines(ctx: CaseContext) -> list[dict[str, str]]:
    """One evidence line per alerting asset, lowest layer first, with the readings the alerts carried."""
    by_asset: dict[str, list[str]] = {}
    for s in ctx.signals:
        key = s["asset_id"] or s["target"] or "-"
        value = (s["labels"] or {}).get("value")
        text = s["alert_id"] + (f" ({value})" if value else "")
        if text not in by_asset.setdefault(key, []):
            by_asset[key].append(text)
    ordered = sorted(by_asset, key=lambda a: -LAYER_OF.get(ctx.kinds.get(a, ""), -1))
    return [{"source": a, "finding": ", ".join(by_asset[a])} for a in ordered]


def deterministic(ctx: CaseContext, skills: Skills, note: str | None = None) -> Finding:
    evidence = _alert_lines(ctx)
    evidence += [
        {"source": f"change ({c['source']})", "finding": f"{c['asset_name']} {c['change']}: {c['before']} -> {c['after']}"}
        for c in ctx.changes[:5]
    ]
    notes = [note] if note else []
    alerting = sorted(
        {s["asset_id"] for s in ctx.signals if s["asset_id"]}, key=lambda a: -LAYER_OF.get(ctx.kinds.get(a, ""), -1)
    )
    root = alerting[0] if alerting else None
    layers = len({LAYER_OF.get(ctx.kinds.get(a, ""), -1) for a in alerting})
    for asset in alerting:
        kind = ctx.kinds.get(asset)
        fired = {s["alert_id"]: s for s in ctx.signals if s["asset_id"] == asset}
        for sid in ctx.candidates:
            skill = skills.get(sid)
            trigger = next((t["alert"] for t in skill.triggers if t["alert"] in fired), None)
            if skill.target_kind != kind or trigger is None:
                continue
            name = asset.split(":", 1)[1]
            if asset != root:
                lowest = f"{root.split(':', 1)[1]} ({ctx.kinds.get(root)})"
                cause = f"{lowest} is the lowest layer alerting, and {trigger} on {name} follows from it"
            elif layers > 1:
                cause = f"{name} ({kind}) is the lowest layer alerting; the alerts above it are its symptoms"
            else:
                cause = f"{trigger} on {name}"
            return Finding(
                method="deterministic",
                summary=f"{len(ctx.signals)} alert(s) across {len(alerting)} asset(s) trace down to {name}. "
                f"{skill.title} answers it; "
                "its dry run checks the cause before anything changes.",
                likely_cause=cause,
                confidence="medium",
                evidence=evidence,
                root_asset=asset,
                skill_id=sid,
                target=name,
                params=skills.params_from_alert(sid, trigger, fired[trigger]["labels"] or {}),
                engineer_should_check="Read the dry run: the skill's own checks decide whether it acts or escalates.",
                notes=notes,
            )
    why = "no skill in the library answers these alerts" if not ctx.candidates else "no skill targets the asset that alerted"
    return Finding(
        method="deterministic",
        summary=f"{ctx.case['title']}. Escalated to an engineer because {why}.",
        likely_cause=f"Unknown; the lowest alerting asset is {root}" if root else "Unknown",
        confidence="low",
        evidence=evidence,
        root_asset=root,
        skill_id=None,
        target=None,
        params={},
        engineer_should_check="Investigate the alerts; if this recurs, add a skill for it to the library.",
        notes=notes,
    )


class ToolBox:
    """The read-only tools, bound to one tenant: every call carries its id."""

    def __init__(self, db, graph, metrics, logs, ctx: CaseContext):
        self.db, self.graph, self.metrics, self.logs, self.ctx = db, graph, metrics, logs, ctx
        self.calls: list[dict[str, Any]] = []

    def _call(self, name: str, args: dict[str, Any]) -> Any:
        t = self.ctx.tenant_id
        if name == "estate_neighbourhood":
            result = self.graph.neighbourhood(t, str(args["asset_id"]), int(args["hops"]))
            return result if result["assets"] else {"error": f"no asset {args['asset_id']} in this customer's estate"}
        if name == "metric":
            if self.metrics is None:
                raise RuntimeError("metrics are not configured")
            return self.metrics.series(args["metric"], args["asset"], int(args["minutes"]))
        if name == "logs":
            if self.logs is None:
                raise RuntimeError("logs are not configured")
            return self.logs.search(args["host"], args["phrase"], int(args["minutes"]))
        if name == "recent_changes":
            with self.db.scope(t) as conn:
                return conn.execute(
                    "SELECT at, source, asset_id, change, before, after FROM graph_changes WHERE tenant_id = %s "
                    "AND at > now() - make_interval(hours => %s) AND (%s = '' OR asset_id = %s) ORDER BY at DESC LIMIT 25",
                    (t, int(args["hours"]), args["asset_id"], args["asset_id"]),
                ).fetchall()
        if name == "past_cases":
            with self.db.scope(t) as conn:
                return conn.execute(
                    "SELECT ref, title, status, opened_at, resolved_at, summary FROM cases WHERE tenant_id = %s "
                    "AND %s = ANY(assets) AND id <> %s ORDER BY opened_at DESC LIMIT 5",
                    (t, args["asset_id"], self.ctx.case["id"]),
                ).fetchall()
        raise ValueError(f"unknown tool {name}")

    def run(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        try:
            result = self._call(name, args)
            self.calls.append({"tool": name, "args": args, "ok": True})
            return json.dumps(result, default=str)[:6000], False
        except Exception as exc:  # the model sees the error and can try something else
            self.calls.append({"tool": name, "args": args, "ok": False, "error": str(exc)[:200]})
            return f"Error: {exc}", True


def _case_message(ctx: CaseContext, deterministic_view: Finding) -> str:
    return json.dumps(
        {
            "case": {k: ctx.case.get(k) for k in ("ref", "title", "severity", "service", "opened_at")},
            "alerts_by_asset_lowest_layer_first": deterministic_view.evidence,
            "asset_kinds": ctx.kinds,
            "skills_triggered_by_these_alerts": ctx.candidates,
            "graph_walk_suggests": {
                "root_asset": deterministic_view.root_asset,
                "skill_id": deterministic_view.skill_id,
                "target": deterministic_view.target,
                "params": deterministic_view.params,
            },
        },
        default=str,
        indent=1,
    )


def llm(ctx: CaseContext, skills: Skills, settings, client: Any, tools: ToolBox) -> Finding:
    baseline = deterministic(ctx, skills)
    system = [
        {
            "type": "text",
            "text": SYSTEM.format(skills=json.dumps(skills.summary(), indent=1)),
            "cache_control": {"type": "ephemeral"},
        }
    ]
    messages: list[dict[str, Any]] = [{"role": "user", "content": _case_message(ctx, baseline)}]
    usage: list[tuple[str, Any]] = []
    response = None

    def fallback(why: str) -> Finding:
        d = deterministic(ctx, skills, why)
        d.usage = usage
        return d

    for turn in range(settings.diagnosis_max_turns):
        last = turn == settings.diagnosis_max_turns - 1
        response = client.beta.messages.create(
            model=settings.diagnosis_model,
            max_tokens=8000,
            system=system,
            tools=TOOLS,
            tool_choice={"type": "none"} if last else {"type": "auto"},
            output_config={"effort": settings.diagnosis_effort, "format": output_schema(skills)},
            messages=messages,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        usage.append((getattr(response, "model", settings.diagnosis_model), response.usage))
        if response.stop_reason == "refusal":
            return fallback("the model declined this case, so the graph walk was used")
        calls = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not calls:
            break
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for call in calls:
            text, is_error = tools.run(call.name, call.input)
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": text, "is_error": is_error})
        messages.append({"role": "user", "content": results})

    text = next((b.text for b in (response.content if response else []) if b.type == "text"), "")
    try:
        answer = json.loads(text)
    except json.JSONDecodeError:
        return fallback("the model's answer was not valid JSON, so the graph walk was used")

    skill_id = None if answer["skill_id"] == "none" else answer["skill_id"]
    target = (answer.get("target") or "").split(":", 1)[-1] or None
    params = {p["name"]: p["value"] for p in answer.get("extra_params", [])}
    if skill_id and skill_id == baseline.skill_id and target == baseline.target:
        params.update(baseline.params)  # values from the alert labels always win over the model's
    notes = [f"{len(tools.calls)} tool call(s): " + ", ".join(c["tool"] for c in tools.calls)] if tools.calls else []
    return Finding(
        method="llm",
        summary=answer["summary"],
        likely_cause=answer["likely_cause"],
        confidence=answer["confidence"],
        evidence=answer["evidence"],
        root_asset=answer.get("root_asset") or baseline.root_asset,
        skill_id=skill_id,
        target=target if skill_id else None,
        params=params if skill_id else {},
        engineer_should_check=answer["engineer_should_check"],
        notes=notes,
        usage=usage,
    )
