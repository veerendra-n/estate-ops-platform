"""Diagnosis agent: from a new case to a cause, evidence and the runbook that answers it.

Two paths with the same output:
- deterministic: the alert names its runbook; parameters come from the alert row. No model call.
- llm: Claude reads the case, can call three read-only tools (estate graph, metrics, logs) for a few
  turns, and answers in a fixed JSON schema. It may only choose a runbook from the catalog, and its
  choice is validated again before anything is proposed.

The llm path falls back to the deterministic one when there is no API key, the tenant is over its AI
budget, the model refuses, or its answer fails validation. Diagnosis never acts on the estate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .. import estate
from ..catalog import Catalog
from ..config import Settings
from ..telemetry import OpenObserve

SYSTEM = """You are the diagnosis agent of a managed IT operations service. A monitoring alert has opened a case \
for one customer. Work out the most likely cause and whether one of the customer's approved runbooks \
answers it.

Rules:
- Use only the facts in the case and what your tools return. Say what you checked; never invent readings.
- You cannot change anything. A runbook you choose is first dry run, then an engineer approves it.
- Choose a runbook only from the catalog below, and only when the evidence supports it. Otherwise choose \
"none" and say what an engineer should look at.
- Parameters the alert already supplies are fixed. Add optional parameters only when the evidence calls \
for it (for example a log shrink target that fits the disk).
- Keep tool calls few: one look at the estate graph and one at the metric that fired is usually enough.
- Write for an engineer on a phone: short sentences, the cause first.

Runbook catalog (ops-foundation):
{catalog}
"""

TOOLS = [
    {
        "name": "estate_neighbourhood",
        "description": "Assets connected to an asset in the customer's estate graph (apps, databases, hosts, "
        "hypervisors, datastores, backup jobs) and how they relate. Asset ids look like host:lab-sql01.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"asset_id": {"type": "string"}, "hops": {"type": "integer", "enum": [1, 2, 3]}},
            "required": ["asset_id", "hops"],
            "additionalProperties": False,
        },
    },
    {
        "name": "metric_series",
        "description": "Recent values of one metric stream for one host, summarised (first, last, max, last 12 "
        "five-minute points). Stream names use underscores, e.g. sqlserver_transaction_log_usage.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "stream": {"type": "string"},
                "host": {"type": "string"},
                "minutes": {"type": "integer", "enum": [30, 60, 240, 1440]},
            },
            "required": ["stream", "host", "minutes"],
            "additionalProperties": False,
        },
    },
    {
        "name": "log_lines",
        "description": "Up to 20 recent log lines from one host containing a phrase (case-insensitive).",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "contains": {"type": "string"},
                "minutes": {"type": "integer", "enum": [30, 60, 240, 1440]},
            },
            "required": ["host", "contains", "minutes"],
            "additionalProperties": False,
        },
    },
]


def output_schema(catalog: Catalog) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "likely_cause": {"type": "string"},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"source": {"type": "string"}, "finding": {"type": "string"}},
                        "required": ["source", "finding"],
                        "additionalProperties": False,
                    },
                },
                "runbook_id": {"type": "string", "enum": [*catalog.runbook_ids(), "none"]},
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
                "evidence",
                "runbook_id",
                "extra_params",
                "engineer_should_check",
            ],
            "additionalProperties": False,
        },
    }


@dataclass
class Diagnosis:
    method: str  # deterministic | llm
    summary: str
    likely_cause: str
    confidence: str
    evidence: list[dict[str, str]]
    runbook_id: str | None
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
            "evidence": self.evidence,
            "runbook_id": self.runbook_id,
            "params": self.params,
            "engineer_should_check": self.engineer_should_check,
            "notes": self.notes,
        }


@dataclass
class CaseContext:
    tenant_id: str
    openobserve_org: str
    case: dict[str, Any]
    alert_id: str
    labels: dict[str, Any]
    candidates: list[str]
    mapped_params: dict[str, Any]
    graph: dict[str, Any] | None


def build_context(conn, catalog: Catalog, tenant, case: dict[str, Any]) -> CaseContext:
    first = conn.execute("SELECT alert_id, labels FROM signals WHERE case_id = %s ORDER BY id LIMIT 1", (case["id"],)).fetchone()
    alert_id, labels = first["alert_id"], first["labels"]
    spec = catalog.alert(alert_id)
    mapped = {}
    if spec:
        for param, column in spec.runbook_params.items():
            if param != "rb_targets" and labels.get(column) not in (None, ""):
                mapped[param] = labels[column]
    graph = None
    if case.get("target"):
        host = estate.host_asset(conn, tenant.id, case["target"])
        if host:
            graph = estate.neighbourhood(conn, tenant.id, host["id"], hops=3)
    return CaseContext(
        tenant_id=tenant.id,
        openobserve_org=tenant.openobserve_org,
        case=case,
        alert_id=alert_id,
        labels=labels,
        candidates=catalog.runbooks_for_alert(alert_id),
        mapped_params=mapped,
        graph=graph,
    )


GRAPH_KINDS = {
    "application": "used by",
    "database": "holds",
    "backup_job": "protected by",
    "sql_agent_job": "jobs",
    "hypervisor_host": "runs on",
    "datastore": "stored on",
}


def _graph_story(graph: dict[str, Any] | None) -> list[dict[str, str]]:
    """One line on what the target is connected to, grouped by kind, for an engineer to read at a glance."""
    if not graph:
        return []
    parts = []
    for kind, label in GRAPH_KINDS.items():
        names = [a["name"] for a in graph["assets"] if a["kind"] == kind]
        if names:
            parts.append(f"{label} {', '.join(names)}")
    text = "; ".join(parts)
    return [{"source": "estate graph", "finding": text[:1].upper() + text[1:]}] if parts else []


def deterministic(ctx: CaseContext, catalog: Catalog, note: str | None = None) -> Diagnosis:
    labels_text = ", ".join(f"{k}={v}" for k, v in ctx.labels.items() if k != "tenant_id")
    evidence = [{"source": f"alert {ctx.alert_id}", "finding": labels_text or "no row values"}] + _graph_story(ctx.graph)
    notes = [note] if note else []
    if len(ctx.candidates) == 1:
        rb = catalog.runbook(ctx.candidates[0])
        return Diagnosis(
            method="deterministic",
            summary=f"{ctx.case['title']}. Its linked runbook ({rb['title']}) checks the cause in a dry run.",
            likely_cause="Not confirmed yet: the runbook's read-only checks establish it in the dry run",
            confidence="medium",
            evidence=evidence,
            runbook_id=rb["id"],
            params=dict(ctx.mapped_params),
            engineer_should_check="Read the dry run: the runbook's own checks decide whether it acts or escalates.",
            notes=notes,
        )
    why = "no runbook answers this alert" if not ctx.candidates else f"{len(ctx.candidates)} runbooks could answer it"
    return Diagnosis(
        method="deterministic",
        summary=f"{ctx.case['title']}. Escalated to an engineer because {why}.",
        likely_cause="Unknown",
        confidence="low",
        evidence=evidence,
        runbook_id=None,
        params={},
        engineer_should_check="Investigate the alert; if this recurs, add a runbook for it in ops-foundation.",
        notes=notes,
    )


class ToolBox:
    """The read-only tools, bound to one tenant. Every call is scoped by that tenant's id and org."""

    def __init__(self, conn, ctx: CaseContext, telemetry: OpenObserve | None):
        self.conn, self.ctx, self.telemetry = conn, ctx, telemetry
        self.calls: list[dict[str, Any]] = []

    def run(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        try:
            if name == "estate_neighbourhood":
                result = estate.neighbourhood(self.conn, self.ctx.tenant_id, str(args["asset_id"]), int(args["hops"]))
                if len(result["assets"]) == 0:
                    result = {"error": f"no asset {args['asset_id']} in this customer's estate"}
            elif name == "metric_series":
                if not self.telemetry:
                    raise RuntimeError("telemetry is not configured")
                result = self.telemetry.metric_series(
                    self.ctx.openobserve_org, args["stream"], args["host"], int(args["minutes"])
                )
            elif name == "log_lines":
                if not self.telemetry:
                    raise RuntimeError("telemetry is not configured")
                result = self.telemetry.log_lines(self.ctx.openobserve_org, args["host"], args["contains"], int(args["minutes"]))
            else:
                raise ValueError(f"unknown tool {name}")
            self.calls.append({"tool": name, "args": args, "ok": True})
            return json.dumps(result, default=str)[:6000], False
        except Exception as exc:  # the model sees the error and can try something else
            self.calls.append({"tool": name, "args": args, "ok": False, "error": str(exc)[:200]})
            return f"Error: {exc}", True


def _case_message(ctx: CaseContext) -> str:
    return json.dumps(
        {
            "case": {k: ctx.case.get(k) for k in ("ref", "title", "severity", "target", "opened_at")},
            "alert_id": ctx.alert_id,
            "alert_row": ctx.labels,
            "runbooks_linked_to_this_alert": ctx.candidates,
            "parameters_from_alert": ctx.mapped_params,
            "target_asset": f"host:{ctx.case['target']}" if ctx.case.get("target") else None,
        },
        default=str,
        indent=1,
    )


def llm(
    ctx: CaseContext,
    catalog: Catalog,
    settings: Settings,
    client: Any,
    tools: ToolBox,
) -> Diagnosis:
    system = [
        {
            "type": "text",
            "text": SYSTEM.format(catalog=json.dumps(catalog.summary(), indent=1)),
            "cache_control": {"type": "ephemeral"},
        }
    ]
    messages: list[dict[str, Any]] = [{"role": "user", "content": _case_message(ctx)}]
    usage: list[tuple[str, Any]] = []
    response = None
    for turn in range(settings.diagnosis_max_turns):
        last = turn == settings.diagnosis_max_turns - 1
        response = client.beta.messages.create(
            model=settings.diagnosis_model,
            max_tokens=8000,
            system=system,
            tools=TOOLS,
            tool_choice={"type": "none"} if last else {"type": "auto"},
            output_config={"effort": settings.diagnosis_effort, "format": output_schema(catalog)},
            messages=messages,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        usage.append((getattr(response, "model", settings.diagnosis_model), response.usage))
        if response.stop_reason == "refusal":
            d = deterministic(ctx, catalog, "the model declined this case, so the alert's own runbook mapping was used")
            d.usage = usage
            return d
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
        d = deterministic(ctx, catalog, "the model's answer was not valid JSON, so the alert's own runbook mapping was used")
        d.usage = usage
        return d

    runbook_id = None if answer["runbook_id"] == "none" else answer["runbook_id"]
    params = {p["name"]: p["value"] for p in answer.get("extra_params", [])}
    params.update(ctx.mapped_params)  # values from the alert row always win over the model's
    notes = [f"{len(tools.calls)} tool call(s): " + ", ".join(c["tool"] for c in tools.calls)] if tools.calls else []
    return Diagnosis(
        method="llm",
        summary=answer["summary"],
        likely_cause=answer["likely_cause"],
        confidence=answer["confidence"],
        evidence=answer["evidence"],
        runbook_id=runbook_id,
        params=params,
        engineer_should_check=answer["engineer_should_check"],
        notes=notes,
        usage=usage,
    )
