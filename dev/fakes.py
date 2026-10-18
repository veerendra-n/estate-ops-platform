"""Stand-ins for Semaphore and OpenObserve, so the whole loop runs on a laptop with no customer estate.

Only the API calls the platform makes are implemented. The fake Semaphore imitates what ops-foundation's
runbook_run role prints: one RUNBOOK_EVIDENCE line per host, dry run unless told otherwise, and the
guard's refusal (a failed task with no evidence) when a live run of an approval-required runbook has no
approval reference.

Run: uvicorn dev.fakes:app --port 8090
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request

CATALOG = Path(os.environ.get("CATALOG_FILE", Path(__file__).resolve().parents[1] / "tests/fixtures/catalog.json"))
POLLS_BEFORE_DONE = int(os.environ.get("FAKE_POLLS_BEFORE_DONE", "1"))

app = FastAPI(title="Fakes: Semaphore and OpenObserve")
_ids = count(1)
_tasks: dict[int, dict[str, Any]] = {}
_overrides: dict[str, dict[str, Any]] = {}


def _runbooks() -> dict[str, dict[str, Any]]:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    return {rb["id"]: rb for rb in data["runbooks"]}


# What each runbook "finds" on the fictitious lab estate. Mirrors the decide.yml cases in ops-foundation.
SCENARIOS: dict[str, dict[str, Any]] = {
    "sql/log-backup-shrink": {
        "before": {
            "database": "FinanceERP",
            "log_used_pct": 96.4,
            "log_size_mb": 61440,
            "log_reuse_wait": "LOG_BACKUP",
            "recovery_model": "FULL",
            "last_log_backup_by": "SQL Agent job LogBackup - FinanceERP",
            "minutes_since_log_backup": 212,
            "agent_job_last_outcome": "Failed (disk quota on backup share)",
            "volume_free_gb": 22,
        },
        "decision": {
            "action": "backup_and_shrink",
            "reason": "Log is only waiting for a log backup; the customer's job owns the chain, so start it and shrink to 4096 MB.",
        },
        "actions": [
            {"step": "start_agent_job", "job": "LogBackup - FinanceERP", "result": "Succeeded"},
            {"step": "shrink_log", "target_mb": 4096, "result": "ok"},
        ],
        "after": {"log_used_pct": 3.1, "log_size_mb": 4096, "log_reuse_wait": "NOTHING"},
        "outcome": "fixed",
    },
    "windows/restart-spooler": {
        "before": {"service": "Spooler", "state": "Stopped", "stuck_jobs": 14},
        "decision": {"action": "restart", "reason": "Spooler stopped with jobs queued; restart and clear stuck jobs."},
        "actions": [{"step": "clear_queue", "removed": 14}, {"step": "start_service", "result": "Running"}],
        "after": {"service": "Spooler", "state": "Running", "stuck_jobs": 0},
        "outcome": "fixed",
    },
}


def _evidence(rb: dict[str, Any], extra: dict[str, Any], target: str) -> dict[str, Any]:
    scenario = (
        _overrides.get(rb["id"])
        or SCENARIOS.get(rb["id"])
        or {
            "before": {},
            "decision": {"action": "escalate", "reason": "the fake has no scenario for this runbook"},
            "actions": [],
            "after": {},
            "outcome": "escalated",
        }
    )
    dry = bool(extra.get("rb_dry_run", True))
    now = datetime.now(UTC).isoformat()
    ev = {
        "schema": "ops-foundation/evidence/v1",
        "runbook": rb["id"],
        "runbook_version": rb["version"],
        "run_id": extra.get("rb_run_id"),
        "tenant": extra.get("rb_tenant"),
        "target": target,
        "dry_run": dry,
        "approval_ref": extra.get("rb_approval_ref"),
        "started_at": now,
        "finished_at": now,
        "before": scenario["before"],
        "decision": scenario["decision"],
        "actions": [],
        "after": {},
        "outcome": "dry_run",
        "error": None,
    }
    action = scenario["decision"]["action"]
    if action in ("none", "escalate"):  # same precedence as roles/runbook_run's "Set outcome"
        ev["outcome"] = "no_action" if action == "none" else "escalated"
    elif not dry:
        ev.update(actions=scenario["actions"], after=scenario["after"], outcome=scenario["outcome"])
    return ev


def _guard_refuses(t: dict[str, Any]) -> bool:
    rb = _runbooks()[t["runbook"]]
    live = not t["extra"].get("rb_dry_run", True)
    return live and rb.get("approval") == "required" and not t["extra"].get("rb_approval_ref")


# ---- Semaphore -------------------------------------------------------------------------------------------


@app.get("/semaphore/api/project/{project_id}/templates")
def templates(project_id: int) -> list[dict[str, Any]]:
    return [
        {"id": i, "name": rb_id, "playbook": rb.get("playbook")} for i, (rb_id, rb) in enumerate(sorted(_runbooks().items()), 1)
    ]


@app.post("/semaphore/api/project/{project_id}/tasks", status_code=201)
async def start_task(project_id: int, request: Request) -> dict[str, Any]:
    body = await request.json()
    by_id = {t["id"]: t["name"] for t in templates(project_id)}
    rb_id = by_id.get(body.get("template_id"))
    if rb_id is None:
        raise HTTPException(400, "unknown template")
    extra = json.loads(body.get("environment") or "{}")
    task_id = next(_ids)
    _tasks[task_id] = {"project": project_id, "runbook": rb_id, "extra": extra, "polls": 0, "created": time.time()}
    return {"id": task_id, "template_id": body["template_id"], "status": "waiting"}


@app.get("/semaphore/api/project/{project_id}/tasks/{task_id}")
def task(project_id: int, task_id: int) -> dict[str, Any]:
    t = _tasks.get(task_id)
    if t is None or t["project"] != project_id:
        raise HTTPException(404, "no such task")
    t["polls"] += 1
    status = "running" if t["polls"] <= POLLS_BEFORE_DONE else ("error" if _guard_refuses(t) else "success")
    return {"id": task_id, "status": status}


@app.get("/semaphore/api/project/{project_id}/tasks/{task_id}/output")
def task_output(project_id: int, task_id: int) -> list[dict[str, Any]]:
    t = _tasks.get(task_id)
    if t is None or t["project"] != project_id:
        raise HTTPException(404, "no such task")
    rb = _runbooks()[t["runbook"]]
    target = t["extra"].get("rb_targets", "unknown")
    if _guard_refuses(t):  # the guard fails the play before any evidence is printed
        return [
            {
                "task_id": task_id,
                "time": datetime.now(UTC).isoformat(),
                "output": 'fatal: [localhost]: FAILED! => {"msg": "rb_approval_ref is required for a live run"}',
            }
        ]
    ev = _evidence(rb, t["extra"], target)
    msg = json.dumps("RUNBOOK_EVIDENCE " + json.dumps(ev))  # Ansible prints the msg as a quoted JSON string
    lines = [
        f"PLAY [{rb['title']}] ****",
        "TASK [runbook_evidence : Emit evidence] ****",
        f"ok: [{target}] => {{",
        f'    "msg": {msg}',
        "}",
        "PLAY RECAP ****",
    ]
    return [{"task_id": task_id, "time": datetime.now(UTC).isoformat(), "output": line} for line in lines]


@app.post("/semaphore/_scenario/{runbook_id:path}")
async def set_scenario(runbook_id: str, request: Request) -> dict[str, str]:
    """Test hook: make the next runs of a runbook find something else (e.g. a decision to escalate)."""
    _overrides[runbook_id] = await request.json()
    return {"ok": runbook_id}


@app.delete("/semaphore/_scenario")
def clear_scenarios() -> dict[str, bool]:
    _overrides.clear()
    return {"ok": True}


# ---- OpenObserve -------------------------------------------------------------------------------------------


@app.post("/openobserve/api/{org}/_search")
async def search(org: str, request: Request) -> dict[str, Any]:
    sql = (await request.json())["query"]["sql"]
    if "sqlserver_transaction_log_usage" in sql:
        hits = [{"t": i, "max_value": round(40 + 56.4 / (1 + math.exp(-(i - 6))), 1)} for i in range(12)]
    elif "str_match_ignore_case" in sql and "lab-sql01" in sql:
        hits = [
            {"_timestamp": 1, "body": "Error 9002: The transaction log for database 'FinanceERP' is full due to 'LOG_BACKUP'."},
            {
                "_timestamp": 2,
                "body": "SQL Agent job 'LogBackup - FinanceERP' failed: Operating system error 112 (not enough space) on \\\\backup01\\sql$.",
            },
        ]
    else:
        hits = []
    return {"took": 3, "hits": hits, "total": len(hits), "org": org}
