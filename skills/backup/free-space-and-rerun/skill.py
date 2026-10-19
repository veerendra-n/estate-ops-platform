"""Free space on a full backup repository by removing orphaned restore points, then rerun the failed job."""

from __future__ import annotations

import math
import time
from typing import Any

SPACE_WORDS = ("space", "full", "capacity")


def gather(ctx) -> dict[str, Any]:
    veeam = ctx.connector("veeam")
    job = veeam.job_by_name(ctx.target)
    if job is None:
        raise LookupError(f"Veeam has no job named {ctx.target}")
    repo_id = (job.get("storage") or {}).get("backupRepositoryId")
    repo = next((r for r in veeam.repositories() if r["id"] == repo_id), None)
    if repo is None:
        raise LookupError(f"the repository of {ctx.target} is not in Veeam's repository list")
    sessions = [s for s in veeam.sessions() if s.get("jobId") == job["id"]]
    last = max(sessions, key=lambda s: s.get("creationTime") or "", default=None)
    orphans = [
        {"id": b["id"], "name": b["name"], "size_gb": b.get("sizeGB", 0)} for b in veeam.backups(repo_id) if not b.get("jobId")
    ]
    return {
        "job": job["name"],
        "job_id": job["id"],
        "repository": repo["name"],
        "capacity_gb": repo["capacityGB"],
        "free_gb": repo["freeGB"],
        "last_session": {
            "id": last["id"],
            "state": last.get("state"),
            "result": ((last.get("result") or {}).get("result") or "").lower(),
            "message": (last.get("result") or {}).get("message", ""),
        }
        if last
        else None,
        "orphans": sorted(orphans, key=lambda o: -o["size_gb"]),
    }


def decide(facts: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    last = facts.get("last_session")
    if last and last["state"] == "Working":
        return {"action": "none", "reason": f"{facts['job']} is running now", "plan": []}
    if last and last["result"] == "success":
        return {"action": "none", "reason": f"the last run of {facts['job']} succeeded", "plan": []}
    need = math.ceil(params["min_free_ratio"] * facts["capacity_gb"]) + params["rerun_gb"]
    about_space = any(w in (last or {}).get("message", "").lower() for w in SPACE_WORDS)
    if facts["free_gb"] >= need:
        if about_space:
            return {
                "action": "rerun",
                "reason": f"{facts['repository']} has {facts['free_gb']} GB free again",
                "plan": [{"rerun": facts["job"]}],
            }
        return {
            "action": "escalate",
            "reason": f"{facts['job']} failed for another reason: {(last or {}).get('message', 'unknown')}",
            "plan": [],
        }
    short = need - facts["free_gb"]
    chosen, freed = [], 0
    for o in facts["orphans"]:
        if freed >= short:
            break
        chosen.append(o)
        freed += o["size_gb"]
    if freed < short:
        return {
            "action": "escalate",
            "reason": f"{facts['repository']} needs {short} GB more and orphaned restore points hold only {freed} GB; "
            "extend the repository",
            "plan": [],
        }
    if freed > params["max_remove_gb"]:
        return {
            "action": "escalate",
            "reason": f"freeing enough would remove {freed} GB, over the {params['max_remove_gb']} GB limit",
            "plan": [],
        }
    return {
        "action": "remove_orphans_and_rerun",
        "reason": (
            f"{facts['repository']} has {facts['free_gb']} GB free of {facts['capacity_gb']} GB; removing {len(chosen)} orphaned "
            f"restore point(s) frees {freed} GB, enough to rerun {facts['job']} and keep {params['min_free_ratio']:.0%} free"
        ),
        "plan": [{"remove": o["name"], "backup_id": o["id"], "size_gb": o["size_gb"]} for o in chosen]
        + [{"rerun": facts["job"]}],
    }


def act(ctx, facts: dict[str, Any], decision: dict[str, Any]) -> list[dict[str, Any]]:
    veeam = ctx.connector("veeam")
    done = []
    for step in decision["plan"]:
        if "remove" in step:
            veeam.remove_backup(step["backup_id"])
            ctx.note(f"removed orphaned restore points {step['remove']} ({step['size_gb']} GB)")
            done.append({"did": "remove_backup", **step})
        else:
            session = veeam.start_job(facts["job_id"])
            ctx.note(f"started {facts['job']} (session {session.get('id')})")
            done.append({"did": "start_job", "job": facts["job"], "session_id": session.get("id")})
    return done


def verify(ctx, facts: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    veeam = ctx.connector("veeam")
    started = next((a for a in ctx.actions if a.get("did") == "start_job"), None)
    deadline = time.monotonic() + ctx.params["verify_minutes"] * 60
    while True:
        sessions = [s for s in veeam.sessions() if s.get("jobId") == facts["job_id"]]
        if started and started.get("session_id"):
            sessions = [s for s in sessions if s["id"] == started["session_id"]] or sessions
        last = max(sessions, key=lambda s: s.get("creationTime") or "", default=None)
        if last and last.get("state") != "Working" and last["id"] != (facts.get("last_session") or {}).get("id"):
            result = ((last.get("result") or {}).get("result") or "").lower()
            repo = next((r for r in veeam.repositories() if r["name"] == facts["repository"]), {})
            out = {"session": last["id"], "result": result, "free_gb": repo.get("freeGB")}
            if result == "success":
                return {"ok": True, **out}
            return {"ok": False, **out, "reason": (last.get("result") or {}).get("message") or f"the rerun ended {result}"}
        if time.monotonic() >= deadline:
            return {"ok": False, "reason": f"{facts['job']} did not finish within {ctx.params['verify_minutes']} minutes"}
        ctx.sleep(10)
