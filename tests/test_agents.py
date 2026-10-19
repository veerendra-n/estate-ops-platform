"""The model paths of the investigator and the copilot, with a scripted stand-in for the Claude client.

These check the harness, not the model: tools stay inside one tenant, alert labels beat the model's
parameters, a pick the platform can't validate is refused, spend is metered, and every failure falls back
to the deterministic graph walk. They run the steps directly, without the workflow around them.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from estate_ops import triage
from estate_ops.api import create_app
from estate_ops.integrations.teams import card_for

CONSOLE = {"Authorization": "Bearer console-test-token"}
DATASTORE = {"source": "metrics", "alert_id": "DatastoreLatencyHigh", "severity": "P2", "rows": [{"datastore": "CTO-DS-CRIT-02"}]}
CHECKOUT = {"source": "metrics", "alert_id": "ServiceLatencyHigh", "severity": "P2", "rows": [{"service": "Checkout"}]}


def usage(i=1200, o=300, cr=0, cw=0):
    return NS(input_tokens=i, output_tokens=o, cache_read_input_tokens=cr, cache_creation_input_tokens=cw)


def tool_use(id_, name, args):
    return NS(type="tool_use", id=id_, name=name, input=args)


def turn(*calls):
    return NS(stop_reason="tool_use", content=list(calls), usage=usage(), model="claude-opus-5-5")


def final(answer, stop="end_turn"):
    return NS(stop_reason=stop, content=[NS(type="text", text=json.dumps(answer))], usage=usage(), model="claude-opus-5-5")


ANSWER = {
    "summary": "CTO-BI-ETL01's full reload saturates CTO-DS-CRIT-02, so Checkout's SQL server waits on disk.",
    "likely_cause": "An unthrottled ETL job on a VM that shares the shop's datastore.",
    "confidence": "high",
    "root_asset": "server:CTO-BI-ETL01",
    "evidence": [
        {"source": "metric datastore_latency_ms", "finding": "38 ms, from 2 ms"},
        {"source": "logs CTO-SHOP-SQL01", "finding": "I/O requests taking longer than 15 seconds"},
    ],
    "skill_id": "vmware/rebalance-datastore",
    "target": "datastore:CTO-DS-CRIT-02",
    "extra_params": [{"name": "max_destination_latency_ms", "value": "6"}],
    "engineer_should_check": "Ask the BI team to throttle the SalesHistoryRebuild job.",
}


class FakeClaude:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))
        self.messages = NS(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(dict(kwargs, messages=list(kwargs["messages"])))  # the SDK serialises at call time
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _investigate(platform, alerts=(DATASTORE, CHECKOUT), tenant="contoso") -> str:
    case_id = None
    for a in alerts:
        with platform.db.scope(tenant) as conn:
            case_id = triage.ingest(conn, platform.tenants.get(tenant), a, str(uuid.uuid4()), platform.graph)[0].case_id
    platform.investigate(tenant, case_id)
    return case_id


def _timeline(platform, case_id, tenant="contoso"):
    with platform.db.scope(tenant) as conn:
        entries = conn.execute("SELECT kind, body FROM ledger WHERE case_id = %s ORDER BY id", (case_id,)).fetchall()
        proposals = conn.execute("SELECT * FROM proposals WHERE case_id = %s", (case_id,)).fetchall()
        spend = conn.execute("SELECT agent, model, cost_usd FROM ai_usage WHERE case_id = %s", (case_id,)).fetchall()
    return entries, proposals, spend


def test_llm_investigation_uses_tools_and_its_pick_is_validated(make_platform, estate):
    estate.post("/_sim/faults/checkout-slow")
    claude = FakeClaude(
        [
            turn(
                tool_use("t1", "metric", {"metric": "datastore_latency_ms", "asset": "CTO-DS-CRIT-02", "minutes": 60}),
                tool_use("t2", "logs", {"host": "CTO-SHOP-SQL01", "phrase": "I/O", "minutes": 60}),
                tool_use("t3", "estate_neighbourhood", {"asset_id": "server:FAB-EHR-SQL01", "hops": 1}),
            ),
            final(ANSWER),
        ]
    )
    platform = make_platform(anthropic=claude)
    case_id = _investigate(platform)
    entries, [proposal], spend = _timeline(platform, case_id)
    finding = next(e["body"] for e in entries if e["kind"] == "investigation")
    assert finding["method"] == "llm" and finding["notes"] == ["3 tool call(s): metric, logs, estate_neighbourhood"]
    assert proposal["skill_id"] == "vmware/rebalance-datastore" and proposal["targets"] == "CTO-DS-CRIT-02"
    assert proposal["params"]["max_destination_latency_ms"] == 6
    results = claude.requests[1]["messages"][-1]["content"]
    assert json.loads(results[0]["content"])["series"][0]["last"] > 20  # the sim's real reading
    assert "15 seconds" in results[1]["content"]
    assert "no asset server:FAB-EHR-SQL01" in results[2]["content"]  # another customer's asset is invisible
    req = claude.requests[0]
    assert req["fallbacks"] == "default" and req["output_config"]["effort"] == "low"
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert [s["agent"] for s in spend] == ["investigator", "investigator"] and all(s["cost_usd"] > 0 for s in spend)


def test_a_pick_that_does_not_match_the_estate_is_escalated(make_platform):
    wrong = dict(ANSWER, skill_id="windows/restart-service", target="CTO-DS-CRIT-02", extra_params=[])
    platform = make_platform(anthropic=FakeClaude([final(wrong)]))
    case_id = _investigate(platform)
    entries, proposals, _ = _timeline(platform, case_id)
    assert proposals == []
    assert entries[-1]["kind"] == "escalated" and "is not a known server" in entries[-1]["body"]["reason"]


def test_model_failures_fall_back_to_the_graph_walk(make_platform):
    for responses, note in [
        ([RuntimeError("overloaded")], "the model call failed (RuntimeError)"),
        ([final({}, stop="refusal")], "the model declined"),
        ([NS(stop_reason="end_turn", content=[NS(type="text", text="not json")], usage=usage(), model="m")], "not valid JSON"),
    ]:
        platform = make_platform(anthropic=FakeClaude(responses))
        case_id = _investigate(platform)
        entries, [proposal], _ = _timeline(platform, case_id)
        finding = next(e["body"] for e in entries if e["kind"] == "investigation")
        assert finding["method"] == "deterministic" and note in finding["notes"][0]
        assert proposal["skill_id"] == "vmware/rebalance-datastore"
        with platform.db.scope("contoso") as conn:  # close it, so the next round opens a new case
            conn.execute("UPDATE cases SET status = 'resolved' WHERE id = %s", (case_id,))


def test_over_budget_customers_get_the_graph_walk(make_platform):
    platform = make_platform(anthropic=FakeClaude([]))
    with platform.db.scope("contoso") as conn:
        conn.execute(
            "INSERT INTO ai_usage (tenant_id, agent, model, input_tokens, output_tokens, cost_usd) "
            "VALUES ('contoso', 'x', 'claude-opus-5-5', 1, 1, 100)"
        )
    low = dict(DATASTORE, severity="P3")
    case_id = _investigate(platform, (low,))
    entries, _, _ = _timeline(platform, case_id)
    finding = next(e["body"] for e in entries if e["kind"] == "investigation")
    assert finding["method"] == "deterministic" and finding["notes"]


def test_copilot_answers_from_the_case_and_meters_cost(make_platform):
    text = NS(type="text", text="Yes: the move is a storage vMotion.")
    answer = NS(stop_reason="end_turn", content=[text], usage=usage(), model="claude-haiku-5-5")
    claude = FakeClaude([final(ANSWER), answer])
    platform = make_platform(anthropic=claude)
    case_id = _investigate(platform)
    with TestClient(create_app(platform)) as api:
        resp = api.post(f"/api/v1/cases/{case_id}/ask", json={"question": "Is it safe to move?", "who": "Priya"}, headers=CONSOLE)
        assert resp.status_code == 200, resp.text
        assert resp.json()["answer"].startswith("Yes")
        d = api.get(f"/api/v1/cases/{case_id}", headers=CONSOLE).json()
    assert [e["kind"] for e in d["timeline"]][-2:] == ["copilot_question", "copilot_answer"]
    assert claude.requests[1]["model"] == "claude-haiku-5-5"
    assert "vmware/rebalance-datastore" in claude.requests[1]["messages"][0]["content"]


def test_teams_cards_for_people():
    from estate_ops.tenancy import Tenant

    t = Tenant(id="contoso", name="Contoso Retail", intake_token_env="X")
    case = {"ref": "CONTOSO-00001", "title": "Datastore slow", "severity": "P2", "target": "CTO-DS-CRIT-02"}
    card = card_for({"kind": "approval_requested", "body": {"reason": "move ETL"}}, case, t, "https://x/cases/1")
    assert card["attachments"][0]["content"]["actions"][0]["url"] == "https://x/cases/1"
    assert card_for({"kind": "resolved", "body": {"reason": "fixed"}}, case, t, "u") is not None
    assert card_for({"kind": "investigation", "body": {}}, case, t, "u") is None
