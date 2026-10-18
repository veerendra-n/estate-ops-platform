"""The model path of the diagnosis agent, with a scripted stand-in for the Claude client.

These check the harness, not the model: tools stay inside one tenant, alert values beat the model's,
spend is metered, and every failure mode falls back to the deterministic diagnosis.
"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

from conftest import SQL_ALERT, drain
from fastapi.testclient import TestClient

from estate_ops.api import create_app

LAB = {"Authorization": "Bearer lab-test-token"}
CONSOLE = {"Authorization": "Bearer console-test-token"}


def usage(i=1200, o=300, cr=0, cw=0):
    return NS(input_tokens=i, output_tokens=o, cache_read_input_tokens=cr, cache_creation_input_tokens=cw)


def tool_use(id_, name, args):
    return NS(type="tool_use", id=id_, name=name, input=args)


def final(answer):
    return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(answer))], usage=usage(), model="claude-opus-5-5")


ANSWER = {
    "summary": "FinanceERP's log on lab-sql01 is 96% full because its log backup job is failing.",
    "likely_cause": "SQL Agent job 'LogBackup - FinanceERP' fails writing to the backup share (out of space).",
    "confidence": "high",
    "evidence": [
        {"source": "metric sqlserver_transaction_log_usage", "finding": "rose from 40% to 96% in an hour"},
        {"source": "logs lab-sql01", "finding": "error 9002 log full due to LOG_BACKUP; job failed with error 112"},
    ],
    "runbook_id": "sql/log-backup-shrink",
    "extra_params": [{"name": "rb_shrink_target_mb", "value": "8192"}, {"name": "rb_database", "value": "master"}],
    "engineer_should_check": "Free space on \\\\backup01\\sql$ or the job will fail again tonight.",
}


class FakeClaude:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(dict(kwargs, messages=list(kwargs["messages"])))  # the SDK serialises at call time
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _run(make_platform, claude, alert=SQL_ALERT):
    platform = make_platform(anthropic=claude)
    api = TestClient(create_app(platform))
    case_id = api.post("/api/v1/intake/openobserve/lab", json=alert, headers=LAB).json()["cases"][0]["case_id"]
    drain(platform)
    return platform, api.get(f"/api/v1/cases/{case_id}", headers=CONSOLE).json()


def test_model_diagnosis_with_tools(make_platform):
    claude = FakeClaude(
        [
            NS(
                stop_reason="tool_use",
                usage=usage(),
                model="claude-opus-5-5",
                content=[
                    tool_use(
                        "t1", "metric_series", {"stream": "sqlserver_transaction_log_usage", "host": "lab-sql01", "minutes": 60}
                    ),
                    tool_use("t2", "log_lines", {"host": "lab-sql01", "contains": "transaction log", "minutes": 60}),
                ],
            ),
            NS(
                stop_reason="tool_use",
                usage=usage(cr=3000),
                model="claude-opus-5-5",
                content=[
                    tool_use("t3", "estate_neighbourhood", {"asset_id": "host:cto-sql01", "hops": 2}),  # another customer's
                ],
            ),
            final(ANSWER),
        ]
    )
    _, detail = _run(make_platform, claude)

    diag = next(e for e in detail["timeline"] if e["kind"] == "diagnosis")["body"]
    assert diag["method"] == "llm" and diag["confidence"] == "high"
    # The alert's database wins over the model's; the model's optional shrink target is kept.
    assert detail["proposals"][0]["params"] == {"rb_database": "FinanceERP", "rb_shrink_target_mb": 8192}
    assert detail["case"]["status"] == "awaiting_approval"
    assert detail["ai_cost_usd"] > 0

    # Tool results went back in one user message per turn; the foreign asset came back as not found.
    second, third = claude.requests[1]["messages"], claude.requests[2]["messages"]
    assert [r["tool_use_id"] for r in second[-1]["content"]] == ["t1", "t2"]
    assert "96.0" in second[-1]["content"][0]["content"]
    assert "no asset host:cto-sql01" in third[-1]["content"][0]["content"]
    # Stable prefix for caching, the cheap effort level, the fixed answer schema and the refusal fallback.
    req = claude.requests[0]
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["output_config"]["effort"] == "low"
    assert "sql/log-backup-shrink" in req["output_config"]["format"]["schema"]["properties"]["runbook_id"]["enum"]
    assert req["fallbacks"] == "default"
    assert all(r["system"] == req["system"] and r["tools"] == req["tools"] for r in claude.requests)


def test_last_turn_forbids_more_tool_calls(make_platform):
    loop = NS(
        stop_reason="tool_use",
        usage=usage(),
        model="claude-opus-5-5",
        content=[tool_use("t", "estate_neighbourhood", {"asset_id": "host:lab-sql01", "hops": 1})],
    )
    claude = FakeClaude([loop, loop, loop, final(ANSWER)])
    _run(make_platform, claude)
    assert [r["tool_choice"]["type"] for r in claude.requests] == ["auto", "auto", "auto", "none"]


def test_invalid_model_parameter_escalates(make_platform):
    bad = dict(ANSWER, extra_params=[{"name": "rb_backup_mode", "value": "delete_everything"}])
    _, detail = _run(make_platform, FakeClaude([final(bad)]))
    assert detail["case"]["status"] == "escalated"
    assert "rb_backup_mode" in detail["timeline"][-1]["body"]["reason"]
    assert detail["proposals"] == []


def test_model_may_decline_to_pick_a_runbook(make_platform):
    _, detail = _run(make_platform, FakeClaude([final(dict(ANSWER, runbook_id="none", extra_params=[]))]))
    assert detail["case"]["status"] == "escalated"


def test_refusal_api_error_and_bad_json_fall_back(make_platform):
    refusal = NS(stop_reason="refusal", content=[], usage=usage(10, 0), model="claude-opus-5-5")
    garbage = NS(stop_reason="end_turn", content=[NS(type="text", text="not json")], usage=usage(), model="claude-opus-5-5")
    for script in ([refusal], [RuntimeError("API down")], [garbage]):
        _, detail = _run(make_platform, FakeClaude(script))
        diag = next(e for e in detail["timeline"] if e["kind"] == "diagnosis")["body"]
        assert diag["method"] == "deterministic", script
        assert detail["case"]["status"] == "awaiting_approval"


def test_over_budget_uses_no_model_except_for_urgent_cases(make_platform):
    platform = make_platform()
    with platform.db.scope("lab") as conn:
        conn.execute(
            "INSERT INTO ai_usage (tenant_id, agent, model, input_tokens, output_tokens, cost_usd) "
            "VALUES ('lab', 'test', 'claude-opus-5-5', 0, 0, 41)"
        )
    platform.db.close()

    spooler = {"alert_id": "print-spooler-stopped", "rows": [{"tenant_id": "lab", "host_name": "lab-prt01"}]}  # P4
    claude = FakeClaude([])
    _, detail = _run(make_platform, claude, spooler)
    assert claude.requests == []
    assert "budget" in next(e for e in detail["timeline"] if e["kind"] == "diagnosis")["body"]["notes"][0]

    claude = FakeClaude([final(ANSWER)])  # the SQL alert is P2, so it still gets the model
    _, detail = _run(make_platform, claude)
    assert len(claude.requests) == 1


def test_deterministic_tenant_never_calls_the_model(make_platform):
    claude = FakeClaude([])
    platform = make_platform(anthropic=claude)
    api = TestClient(create_app(platform))
    payload = {"alert_id": "print-spooler-stopped", "rows": [{"tenant_id": "contoso", "host_name": "cto-prt01"}]}
    api.post("/api/v1/intake/openobserve/contoso", json=payload, headers={"Authorization": "Bearer contoso-test-token"})
    drain(platform)
    assert claude.requests == []
