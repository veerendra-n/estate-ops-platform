"""Case copilot: answers an engineer's question about one case, in plain prose, with the evidence it used.

It sees what the cockpit shows (the case, its alerts, proposals and conversation) and can call the same
read-only tools as the investigator, bound to the case's customer. It can't change anything and can't see
other customers. Each question and answer goes into the case
ledger, so the conversation shows who asked what and what the copilot said.

Cost: a small model by default, a byte-stable system prompt and tool list (cached), at most three tool
turns, and the customer's monthly AI budget applies as it does to investigation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .investigator import TOOLS, CaseContext, ToolBox

SYSTEM = """You are the copilot in the cockpit of a managed IT operations service. An engineer is working one \
case for one customer and asks you a question about it.

Rules:
- Answer from the case record and what your tools return. If they don't answer the question, say so and \
say what to check. Never invent readings, names or history.
- You cannot change anything. Changes happen only through skills that policy allows and people approve.
- Lead with the answer, in at most 120 words of plain prose. Name the case refs, assets and skills you rely on.
- Use tools only when the case record doesn't already answer the question; one or two calls is usually enough.
"""

MAX_TURNS = 4


@dataclass
class Answer:
    text: str
    method: str  # llm | unavailable
    tools: list[dict[str, Any]] = field(default_factory=list)
    usage: list[tuple[str, Any]] = field(default_factory=list)
    model: str | None = None


def tools_for(db, graph, metrics, logs, tenant, case: dict[str, Any]) -> ToolBox:
    """The investigator's read-only tools, bound to the case's customer."""
    return ToolBox(db, graph, metrics, logs, CaseContext(tenant.id, case, [], {}, [], []))


def case_record(detail: dict[str, Any]) -> str:
    """The case as the console shows it, trimmed to what helps an answer."""
    case = detail["case"]
    timeline = [
        {"at": e["at"], "actor": e["actor"], "kind": e["kind"], "body": e["body"]}
        for e in detail["timeline"]
        if e["kind"] not in ("copilot_question", "copilot_answer")
    ][-30:]
    record = {
        "case": {k: case.get(k) for k in ("ref", "title", "severity", "status", "service", "assets", "summary", "opened_at")},
        "customer": case.get("tenant_name"),
        "signals": detail["signals"][-10:],
        "proposals": [
            {k: p.get(k) for k in ("skill_id", "skill_version", "targets", "params", "risk", "mode", "status", "outcome")}
            for p in detail["proposals"]
        ],
        "conversation": timeline,
    }
    return json.dumps(record, default=str)[:24000]


def ask(client: Any, model: str, record: str, history: list[dict[str, str]], question: str, tools: ToolBox) -> Answer:
    """One answer. history holds earlier questions and answers on this case, oldest first."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": f"Case record:\n{record}"}]
    messages.append({"role": "assistant", "content": "I have the case record. What would you like to know?"})
    for turn in history[-6:]:
        messages.append({"role": "user", "content": turn["question"]})
        messages.append({"role": "assistant", "content": turn["answer"]})
    messages.append({"role": "user", "content": question})
    system = [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}]
    usage: list[tuple[str, Any]] = []
    response = None
    for turn in range(MAX_TURNS):
        last = turn == MAX_TURNS - 1
        response = client.messages.create(
            model=model,
            max_tokens=1500,
            system=system,
            tools=TOOLS,
            tool_choice={"type": "none"} if last else {"type": "auto"},
            messages=messages,
        )
        usage.append((getattr(response, "model", model), response.usage))
        if response.stop_reason == "refusal":
            return Answer("The model declined to answer this one. Try asking it another way.", "llm", tools.calls, usage, model)
        calls = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not calls:
            break
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for call in calls:
            text, is_error = tools.run(call.name, call.input)
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": text, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    text = "\n".join(b.text for b in (response.content if response else []) if b.type == "text").strip()
    return Answer(text or "No answer came back. Try again.", "llm", tools.calls, usage, model)
