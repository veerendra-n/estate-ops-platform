"""The case workflow: diagnose, propose, dry run, approve, run, verify. Each step is one job.

    alert -> case (triage) -> diagnose -> proposal -> dry run -> approval -> live run -> resolved
                                       `-> escalated (no runbook, invalid proposal, runbook escalates)

Every step writes to the ledger, which is also the case conversation in the console.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from . import budget, estate, jobs, ledger, policy
from .agents import diagnosis
from .catalog import Catalog
from .config import Settings
from .db import Database
from .semaphore import Semaphore
from .telemetry import OpenObserve
from .tenancy import Tenants

# Runbook outcome -> (proposal status, case status) after a live run.
LIVE_OUTCOMES = {
    "fixed": ("done", "resolved"),
    "no_action": ("done", "resolved"),
    "pending_device": ("done", "running"),
    "escalated": ("done", "escalated"),
    "failed": ("failed", "escalated"),
}


class WorkflowError(RuntimeError):
    pass


@dataclass
class Platform:
    settings: Settings
    db: Database
    tenants: Tenants
    catalog: Catalog
    semaphore: Semaphore
    telemetry: OpenObserve | None
    anthropic: Any | None  # anthropic.Anthropic, or None to run without a model

    # ---- jobs -------------------------------------------------------------------------------------------

    def handle(self, job: dict[str, Any]) -> str:
        """Run one job. Returns "done", or "waiting" when it was put back to poll again."""
        kind, payload = job["kind"], job["payload"]
        if kind == "diagnose":
            self.diagnose(job["tenant_id"], payload["case_id"])
        elif kind == "start_run":
            self.start_run(job["tenant_id"], payload["proposal_id"], payload["live"])
        elif kind == "poll_run":
            done = self.poll_run(job["tenant_id"], payload)
            if not done:
                jobs.reschedule(self.db, job, payload, self.settings.run_poll_seconds)
                return "waiting"
        else:
            raise WorkflowError(f"unknown job kind {kind}")
        jobs.finish(self.db, job["id"])
        return "done"

    # ---- diagnose and propose ---------------------------------------------------------------------------

    def diagnose(self, tenant_id: str, case_id: str) -> None:
        tenant = self.tenants.get(tenant_id)
        with self.db.scope(tenant_id) as conn:
            case = conn.execute("SELECT * FROM cases WHERE id = %s FOR UPDATE", (case_id,)).fetchone()
            # "diagnosing" too, so a retried job after a crash picks the case up again.
            if case is None or case["status"] not in ("open", "diagnosing"):
                return
            conn.execute("UPDATE cases SET status = 'diagnosing', updated_at = now() WHERE id = %s", (case_id,))
            ctx = diagnosis.build_context(conn, self.catalog, tenant, case)
            use_model, budget_note = budget.allows_model(conn, tenant, case["severity"])

        note = None
        if tenant.diagnosis == "deterministic":
            result = diagnosis.deterministic(ctx, self.catalog)
        elif self.anthropic is None:
            result = diagnosis.deterministic(ctx, self.catalog, "no model is configured, so the alert's runbook mapping was used")
        elif not use_model:
            result = diagnosis.deterministic(ctx, self.catalog, budget_note)
        else:
            with self.db.scope(tenant_id) as conn:
                try:
                    result = diagnosis.llm(
                        ctx, self.catalog, self.settings, self.anthropic, diagnosis.ToolBox(conn, ctx, self.telemetry)
                    )
                except Exception as exc:  # API outage must not stall the case
                    result = diagnosis.deterministic(
                        ctx,
                        self.catalog,
                        f"the model call failed ({type(exc).__name__}), so the alert's runbook mapping was used",
                    )
            note = budget_note

        with self.db.scope(tenant_id) as conn:
            spent = sum(
                (
                    budget.record(conn, self.settings, tenant_id, case_id, "diagnosis", model, usage)
                    for model, usage in result.usage
                ),
                start=Decimal(0),
            )
            body = result.as_body() | {"ai_cost_usd": float(spent), "budget": note}
            ledger.append(conn, tenant_id, case_id, "agent:diagnosis", "diagnosis", body)
            conn.execute("UPDATE cases SET summary = %s, updated_at = now() WHERE id = %s", (result.summary, case_id))
            self._propose(conn, tenant_id, case, result)

    def _propose(self, conn, tenant_id: str, case: dict[str, Any], result: diagnosis.Diagnosis) -> None:
        tenant = self.tenants.get(tenant_id)
        case_id = str(case["id"])
        problems: list[str] = []
        if not result.runbook_id:
            problems.append("no runbook was chosen")
        target = case.get("target")
        if not target:
            problems.append("the alert did not name a target")
        elif estate.host_asset(conn, tenant_id, target) is None:
            problems.append(f"{target} is not a known server of {tenant.name}")
        if problems:
            return self._escalate(conn, tenant_id, case_id, "; ".join(problems), result.engineer_should_check)

        runbook = self.catalog.runbook(result.runbook_id)
        params, errors = self.catalog.validate_params(runbook["id"], result.params)
        if errors:
            return self._escalate(
                conn, tenant_id, case_id, "proposal failed validation: " + "; ".join(errors), result.engineer_should_check
            )

        decision = policy.decide(tenant, runbook, [target])
        proposal = conn.execute(
            "INSERT INTO proposals (tenant_id, case_id, runbook_id, runbook_version, catalog_commit, targets, params, "
            "risk, mode, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                tenant_id,
                case_id,
                runbook["id"],
                runbook["version"],
                self.catalog.commit,
                target,
                _json(params),
                runbook["risk"],
                decision.mode,
                "proposed" if decision.mode == "recommend" else "dry_running",
            ),
        ).fetchone()
        ledger.append(
            conn,
            tenant_id,
            case_id,
            "agent:remediator",
            "proposal",
            {
                "proposal_id": str(proposal["id"]),
                "runbook_id": runbook["id"],
                "runbook_title": runbook["title"],
                "runbook_version": runbook["version"],
                "targets": target,
                "params": params,
                "risk": runbook["risk"],
                "approval": runbook["approval"],
                "mode": decision.mode,
                "policy": decision.reason,
                "rollback": runbook.get("rollback"),
            },
        )
        if decision.mode == "recommend":
            conn.execute("UPDATE cases SET status = 'awaiting_approval', updated_at = now() WHERE id = %s", (case_id,))
        else:
            conn.execute("UPDATE cases SET status = 'running', updated_at = now() WHERE id = %s", (case_id,))
            jobs.enqueue(conn, tenant_id, "start_run", {"proposal_id": str(proposal["id"]), "live": False})

    def _escalate(self, conn, tenant_id: str, case_id: str, reason: str, check: str = "") -> None:
        ledger.append(conn, tenant_id, case_id, "platform", "escalated", {"reason": reason, "engineer_should_check": check})
        conn.execute("UPDATE cases SET status = 'escalated', updated_at = now() WHERE id = %s", (case_id,))

    # ---- runs ---------------------------------------------------------------------------------------------

    def start_run(self, tenant_id: str, proposal_id: str, live: bool) -> None:
        tenant = self.tenants.get(tenant_id)
        with self.db.scope(tenant_id) as conn:
            p = conn.execute("SELECT * FROM proposals WHERE id = %s", (proposal_id,)).fetchone()
            case = conn.execute("SELECT ref FROM cases WHERE id = %s", (p["case_id"],)).fetchone()
        expected = "approved" if live else "dry_running"
        if p["status"] != expected:
            raise WorkflowError(f"proposal {proposal_id} is {p['status']}, not {expected}")
        run_id = f"{case['ref']}/{proposal_id[:8]}/{'live' if live else 'dry'}"
        extra_vars: dict[str, Any] = {
            "rb_tenant": tenant_id,
            "rb_targets": p["targets"],
            "rb_dry_run": not live,
            "rb_run_id": run_id,
            **p["params"],
        }
        if live:
            extra_vars["rb_approval_ref"] = f"{case['ref']}/{proposal_id}"
        task_id = self.semaphore.start(tenant.semaphore_project_id, p["runbook_id"], extra_vars)
        with self.db.scope(tenant_id) as conn:
            conn.execute("UPDATE proposals SET status = %s WHERE id = %s", ("running" if live else "dry_running", proposal_id))
            ledger.append(
                conn,
                tenant_id,
                p["case_id"],
                "platform",
                "run_started",
                {
                    "proposal_id": proposal_id,
                    "runbook_id": p["runbook_id"],
                    "dry_run": not live,
                    "semaphore_task_id": task_id,
                    "run_id": run_id,
                },
            )
            jobs.enqueue(
                conn,
                tenant_id,
                "poll_run",
                {"proposal_id": proposal_id, "task_id": task_id, "live": live, "started": time.time()},
                delay_s=self.settings.run_poll_seconds,
            )

    def poll_run(self, tenant_id: str, payload: dict[str, Any]) -> bool:
        """Returns False while the Semaphore task is still running."""
        tenant = self.tenants.get(tenant_id)
        state = self.semaphore.state(tenant.semaphore_project_id, payload["task_id"])
        timed_out = time.time() - payload["started"] > self.settings.run_timeout_seconds
        if not state.finished and not timed_out:
            return False
        proposal_id, live = payload["proposal_id"], payload["live"]
        with self.db.scope(tenant_id) as conn:
            p = conn.execute("SELECT * FROM proposals WHERE id = %s FOR UPDATE", (proposal_id,)).fetchone()
            case_id = str(p["case_id"])
            evidence = [e for e in state.evidence if e.get("tenant") == tenant_id and e.get("target") == p["targets"]]
            dropped = len(state.evidence) - len(evidence)
            item = evidence[0] if evidence else None
            outcome = item["outcome"] if item else ("timed_out" if timed_out else "failed")
            ledger.append(
                conn,
                tenant_id,
                case_id,
                f"runbook:{p['runbook_id']}",
                "run_result",
                {
                    "proposal_id": proposal_id,
                    "dry_run": not live,
                    "semaphore_status": state.status,
                    "outcome": outcome,
                    "evidence": item,
                    "evidence_rejected": dropped,
                },
            )
            if not live:
                self._after_dry_run(conn, tenant_id, p, item, outcome)
            else:
                prop_status, case_status = LIVE_OUTCOMES.get(outcome, ("failed", "escalated"))
                conn.execute(
                    "UPDATE proposals SET status = %s, outcome = %s, evidence = %s WHERE id = %s",
                    (prop_status, outcome, _json(item), proposal_id),
                )
                conn.execute(
                    "UPDATE cases SET status = %s, updated_at = now(), "
                    "resolved_at = CASE WHEN %s = 'resolved' THEN now() END WHERE id = %s",
                    (case_status, case_status, case_id),
                )
                if case_status == "escalated":
                    reason = (item or {}).get("decision", {}).get("reason") or (item or {}).get("error") or outcome
                    ledger.append(conn, tenant_id, case_id, "platform", "escalated", {"reason": f"live run: {reason}"})
        return True

    def _after_dry_run(self, conn, tenant_id: str, p: dict[str, Any], item: dict[str, Any] | None, outcome: str) -> None:
        case_id = str(p["case_id"])
        action = ((item or {}).get("decision") or {}).get("action")
        reason = ((item or {}).get("decision") or {}).get("reason", "")
        conn.execute("UPDATE proposals SET dry_run_evidence = %s WHERE id = %s", (_json(item), p["id"]))
        # roles/runbook_run reports escalated or no_action ahead of dry_run, so read the decision itself.
        if outcome not in ("dry_run", "escalated", "no_action") or action is None:
            conn.execute("UPDATE proposals SET status = 'failed', outcome = %s WHERE id = %s", (outcome, p["id"]))
            return self._escalate(conn, tenant_id, case_id, f"dry run did not complete ({outcome})")
        if action == "none":
            conn.execute("UPDATE proposals SET status = 'done', outcome = 'no_action' WHERE id = %s", (p["id"],))
            conn.execute(
                "UPDATE cases SET status = 'resolved', resolved_at = now(), updated_at = now() WHERE id = %s", (case_id,)
            )
            ledger.append(conn, tenant_id, case_id, "platform", "resolved", {"reason": f"nothing to do: {reason}"})
            return
        if action == "escalate":
            conn.execute("UPDATE proposals SET status = 'done', outcome = 'escalated' WHERE id = %s", (p["id"],))
            return self._escalate(conn, tenant_id, case_id, f"the runbook's own checks escalated: {reason}")
        if p["mode"] == "run_and_notify":
            conn.execute(
                "UPDATE proposals SET status = 'approved', decided_by = 'policy', decided_at = now() WHERE id = %s", (p["id"],)
            )
            ledger.append(
                conn, tenant_id, case_id, "policy", "approved", {"proposal_id": str(p["id"]), "by": "policy", "action": action}
            )
            jobs.enqueue(conn, tenant_id, "start_run", {"proposal_id": str(p["id"]), "live": True})
            return
        conn.execute("UPDATE proposals SET status = 'awaiting_approval' WHERE id = %s", (p["id"],))
        conn.execute("UPDATE cases SET status = 'awaiting_approval', updated_at = now() WHERE id = %s", (case_id,))
        ledger.append(
            conn,
            tenant_id,
            case_id,
            "platform",
            "approval_requested",
            {"proposal_id": str(p["id"]), "action": action, "reason": reason},
        )

    # ---- people ---------------------------------------------------------------------------------------------

    def decide(self, tenant_id: str, proposal_id: str, approve: bool, who: str, note: str = "") -> dict[str, Any]:
        if not who.strip():
            raise WorkflowError("an approval needs a named engineer")
        with self.db.scope(tenant_id) as conn:
            p = conn.execute("SELECT * FROM proposals WHERE id = %s FOR UPDATE", (proposal_id,)).fetchone()
            if p is None:
                raise LookupError("no such proposal")
            if p["status"] != "awaiting_approval":
                raise WorkflowError(f"proposal is {p['status']}; only one awaiting approval can be decided")
            status = "approved" if approve else "rejected"
            conn.execute(
                "UPDATE proposals SET status = %s, decided_by = %s, decided_at = now() WHERE id = %s",
                (status, who, proposal_id),
            )
            ledger.append(conn, tenant_id, p["case_id"], f"engineer:{who}", status, {"proposal_id": proposal_id, "note": note})
            if approve:
                conn.execute("UPDATE cases SET status = 'running', updated_at = now() WHERE id = %s", (p["case_id"],))
                jobs.enqueue(conn, tenant_id, "start_run", {"proposal_id": proposal_id, "live": True})
            else:
                self._escalate(conn, tenant_id, str(p["case_id"]), f"{who} rejected the runbook" + (f": {note}" if note else ""))
            return {"proposal_id": proposal_id, "status": status}


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str)
