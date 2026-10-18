"""The steps of a case: diagnose, propose, dry run, approve, run, verify.

workflows.CaseWorkflow decides the order and holds the waiting; each method here is one step it runs as an
activity. A step returns what should happen next ({"next": "dry_run" | "approval" | "live" | "end",
"proposal_id": ...}) and is safe to run twice, because Temporal retries a step whose worker died.
Every step writes to the ledger, which is also the case conversation in the console.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from . import budget, ledger, policy
from .agents import diagnosis
from .catalog import Catalog
from .config import Settings
from .db import Database
from .estate import EstateGraph
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


Step = dict[str, Any]
END: Step = {"next": "end", "proposal_id": None}


class WorkflowError(RuntimeError):
    pass


@dataclass
class Platform:
    settings: Settings
    db: Database
    graph: EstateGraph
    tenants: Tenants
    catalog: Catalog
    semaphore: Semaphore
    telemetry: OpenObserve | None
    anthropic: Any | None  # anthropic.Anthropic, or None to run without a model

    # ---- diagnose and propose ---------------------------------------------------------------------------

    def diagnose(self, tenant_id: str, case_id: str) -> Step:
        tenant = self.tenants.get(tenant_id)
        with self.db.scope(tenant_id) as conn:
            case = conn.execute("SELECT * FROM cases WHERE id = %s FOR UPDATE", (case_id,)).fetchone()
            if case is None:
                raise LookupError("no such case")
            # "diagnosing" too, so a step retried after a crash picks the case up again.
            if case["status"] not in ("open", "diagnosing"):
                return self._resume(conn, case_id)
            conn.execute("UPDATE cases SET status = 'diagnosing', updated_at = now() WHERE id = %s", (case_id,))
            ctx = diagnosis.build_context(conn, self.catalog, tenant, case, self.graph)
            use_model, budget_note = budget.allows_model(conn, tenant, case["severity"])

        note = None
        if tenant.diagnosis == "deterministic":
            result = diagnosis.deterministic(ctx, self.catalog)
        elif self.anthropic is None:
            result = diagnosis.deterministic(ctx, self.catalog, "no model is configured, so the alert's runbook mapping was used")
        elif not use_model:
            result = diagnosis.deterministic(ctx, self.catalog, budget_note)
        else:
            try:
                result = diagnosis.llm(
                    ctx, self.catalog, self.settings, self.anthropic, diagnosis.ToolBox(self.graph, ctx, self.telemetry)
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
            return self._propose(conn, tenant_id, case, result)

    def _resume(self, conn, case_id: str) -> Step:
        """What comes next for a case whose step already ran, from its latest proposal."""
        p = conn.execute(
            "SELECT id, status FROM proposals WHERE case_id = %s ORDER BY created_at DESC LIMIT 1", (case_id,)
        ).fetchone()
        nxt = {"dry_running": "dry_run", "awaiting_approval": "approval", "approved": "live"}.get(p["status"]) if p else None
        return {"next": nxt, "proposal_id": str(p["id"])} if nxt else END

    def _propose(self, conn, tenant_id: str, case: dict[str, Any], result: diagnosis.Diagnosis) -> Step:
        tenant = self.tenants.get(tenant_id)
        case_id = str(case["id"])
        problems: list[str] = []
        if not result.runbook_id:
            problems.append("no runbook was chosen")
        target = case.get("target")
        if not target:
            problems.append("the alert did not name a target")
        elif self.graph.host(tenant_id, target) is None:
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
            # Recommend only: an engineer runs it by hand, so the workflow ends here.
            conn.execute("UPDATE cases SET status = 'awaiting_approval', updated_at = now() WHERE id = %s", (case_id,))
            return END
        conn.execute("UPDATE cases SET status = 'running', updated_at = now() WHERE id = %s", (case_id,))
        return {"next": "dry_run", "proposal_id": str(proposal["id"])}

    def _escalate(self, conn, tenant_id: str, case_id: str, reason: str, check: str = "") -> Step:
        ledger.append(conn, tenant_id, case_id, "platform", "escalated", {"reason": reason, "engineer_should_check": check})
        conn.execute("UPDATE cases SET status = 'escalated', updated_at = now() WHERE id = %s", (case_id,))
        return END

    def escalate(self, tenant_id: str, case_id: str, reason: str) -> Step:
        """The workflow's last resort when a step keeps failing."""
        with self.db.scope(tenant_id) as conn:
            case = conn.execute("SELECT status FROM cases WHERE id = %s FOR UPDATE", (case_id,)).fetchone()
            if case and case["status"] not in ("resolved", "escalated"):
                self._escalate(conn, tenant_id, case_id, reason)
        return END

    # ---- runs ---------------------------------------------------------------------------------------------

    def start_run(self, tenant_id: str, proposal_id: str, live: bool) -> int:
        """Start the Semaphore task and return its id. A retry after the start was recorded returns the same task."""
        tenant = self.tenants.get(tenant_id)
        with self.db.scope(tenant_id) as conn:
            p = conn.execute("SELECT * FROM proposals WHERE id = %s", (proposal_id,)).fetchone()
            if p is None:
                raise LookupError("no such proposal")
            case = conn.execute("SELECT ref FROM cases WHERE id = %s", (p["case_id"],)).fetchone()
            started = conn.execute(
                "SELECT body FROM ledger WHERE case_id = %s AND kind = 'run_started' "
                "AND body->>'proposal_id' = %s AND (body->>'dry_run')::boolean = %s ORDER BY id DESC LIMIT 1",
                (p["case_id"], proposal_id, not live),
            ).fetchone()
        if started:
            return int(started["body"]["semaphore_task_id"])
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
        return task_id

    def poll_run(self, tenant_id: str, proposal_id: str, task_id: int, live: bool, started: float) -> Step | None:
        """None while the Semaphore task is still running; then records the result and says what comes next."""
        tenant = self.tenants.get(tenant_id)
        state = self.semaphore.state(tenant.semaphore_project_id, task_id)
        timed_out = time.time() - started > self.settings.run_timeout_seconds
        if not state.finished and not timed_out:
            return None
        with self.db.scope(tenant_id) as conn:
            p = conn.execute("SELECT * FROM proposals WHERE id = %s FOR UPDATE", (proposal_id,)).fetchone()
            case_id = str(p["case_id"])
            if p["status"] != ("running" if live else "dry_running"):
                return self._resume(conn, case_id)  # this result was already recorded
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
                return self._after_dry_run(conn, tenant_id, p, item, outcome)
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
        return END

    def _after_dry_run(self, conn, tenant_id: str, p: dict[str, Any], item: dict[str, Any] | None, outcome: str) -> Step:
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
            return END
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
            return {"next": "live", "proposal_id": str(p["id"])}
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
        return {"next": "approval", "proposal_id": str(p["id"])}

    # ---- people ---------------------------------------------------------------------------------------------

    def decide(self, tenant_id: str, proposal_id: str, approve: bool, who: str, note: str = "") -> dict[str, Any]:
        """Record an engineer's decision. Reached only through the workflow's "decide" update."""
        if not who.strip():
            raise WorkflowError("an approval needs a named engineer")
        status = "approved" if approve else "rejected"
        with self.db.scope(tenant_id) as conn:
            p = conn.execute("SELECT * FROM proposals WHERE id = %s FOR UPDATE", (proposal_id,)).fetchone()
            if p is None:
                raise LookupError("no such proposal")
            if p["status"] == status and p["decided_by"] == who:  # a retried step
                return {"proposal_id": proposal_id, "status": status, "next": "live" if approve else "end"}
            if p["status"] != "awaiting_approval":
                raise WorkflowError(f"proposal is {p['status']}; only one awaiting approval can be decided")
            conn.execute(
                "UPDATE proposals SET status = %s, decided_by = %s, decided_at = now() WHERE id = %s",
                (status, who, proposal_id),
            )
            ledger.append(conn, tenant_id, p["case_id"], f"engineer:{who}", status, {"proposal_id": proposal_id, "note": note})
            if approve:
                conn.execute("UPDATE cases SET status = 'running', updated_at = now() WHERE id = %s", (p["case_id"],))
            else:
                self._escalate(conn, tenant_id, str(p["case_id"]), f"{who} rejected the runbook" + (f": {note}" if note else ""))
            return {"proposal_id": proposal_id, "status": status, "next": "live" if approve else "end"}

    def expire_approval(self, tenant_id: str, proposal_id: str, waited_seconds: float) -> Step:
        with self.db.scope(tenant_id) as conn:
            p = conn.execute("SELECT * FROM proposals WHERE id = %s FOR UPDATE", (proposal_id,)).fetchone()
            if p is None or p["status"] != "awaiting_approval":
                return END
            conn.execute(
                "UPDATE proposals SET status = 'rejected', decided_by = 'timeout', decided_at = now() WHERE id = %s",
                (proposal_id,),
            )
            hours = waited_seconds / 3600
            return self._escalate(
                conn, tenant_id, str(p["case_id"]), f"no engineer decided within {hours:g} hours", "Decide by hand or re-run."
            )


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str)
