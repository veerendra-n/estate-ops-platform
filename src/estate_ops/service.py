"""The steps of a case: investigate, propose, dry run, approve, run, verify.

workflows.CaseWorkflow decides the order and holds the waiting; each method here is one step it runs as an
activity. A step returns what should happen next ({"next": "dry_run" | "approval" | "live" | "end",
"proposal_id": ...}) and is safe to run twice, because Temporal retries a step whose worker died.
Every step writes to the ledger, which is also the case conversation in the cockpit.

    investigator  agents/investigator.py: root cause and the skill that answers it
    remediator    _propose(): validates the pick, measures the blast radius, asks OPA how far it may go
    executor      run_skill(): the skill's dry run, then (after approval or policy) its live run
    verifier      the skill's verify(), recorded as its own ledger entry
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from . import budget, ledger
from .agents import investigator
from .config import Settings
from .connectors import Connectors
from .db import Database
from .estate import EstateGraph
from .policy import Policy, build_input
from .skills import Context, Skills
from .skills import run as run_skill_module
from .tenancy import Tenant, Tenants

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
    skills: Skills
    policy: Policy
    anthropic: Any | None  # anthropic.Anthropic, or None to run without a model
    http: Any | None = None  # an httpx.Client for connectors; tests pass one bound to the simulator

    def connectors(self, tenant: Tenant) -> Connectors:
        return Connectors(tenant, self.settings, http=self.http)

    # ---- investigate and propose --------------------------------------------------------------------------

    def investigate(self, tenant_id: str, case_id: str) -> Step:
        tenant = self.tenants.get(tenant_id)
        with self.db.scope(tenant_id) as conn:
            case = conn.execute("SELECT * FROM cases WHERE id = %s FOR UPDATE", (case_id,)).fetchone()
            if case is None:
                raise LookupError("no such case")
            # "diagnosing" too, so a step retried after a crash picks the case up again.
            if case["status"] not in ("open", "diagnosing"):
                return self._resume(conn, case_id)
            conn.execute("UPDATE cases SET status = 'diagnosing', updated_at = now() WHERE id = %s", (case_id,))
            ctx = investigator.build_context(conn, self.skills, tenant, case, self.graph)
            use_model, budget_note = budget.allows_model(conn, tenant, case["severity"])

        note = None
        if tenant.diagnosis == "deterministic":
            finding = investigator.deterministic(ctx, self.skills)
        elif self.anthropic is None:
            finding = investigator.deterministic(ctx, self.skills, "no model is configured, so the graph walk was used")
        elif not use_model:
            finding = investigator.deterministic(ctx, self.skills, budget_note)
        else:
            conns = self.connectors(tenant)
            tools = investigator.ToolBox(self.db, self.graph, conns.metrics(), conns.logs(), ctx)
            try:
                finding = investigator.llm(ctx, self.skills, self.settings, self.anthropic, tools)
            except Exception as exc:  # an API outage must not stall the case
                finding = investigator.deterministic(
                    ctx, self.skills, f"the model call failed ({type(exc).__name__}), so the graph walk was used"
                )
            note = budget_note

        with self.db.scope(tenant_id) as conn:
            spent = sum(
                (budget.record(conn, self.settings, tenant_id, case_id, "investigator", m, u) for m, u in finding.usage),
                start=Decimal(0),
            )
            body = finding.as_body() | {"ai_cost_usd": float(spent), "budget": note, "alerts": len(ctx.signals)}
            ledger.append(conn, tenant_id, case_id, "agent:investigator", "investigation", body)
            conn.execute(
                "UPDATE cases SET summary = %s, responded_at = coalesce(responded_at, now()), updated_at = now() WHERE id = %s",
                (finding.summary, case_id),
            )
            return self._propose(conn, tenant, case, finding)

    def _resume(self, conn, case_id: str) -> Step:
        """What comes next for a case whose step already ran, from its latest proposal."""
        p = conn.execute(
            "SELECT id, status FROM proposals WHERE case_id = %s ORDER BY created_at DESC LIMIT 1", (case_id,)
        ).fetchone()
        nxt = {"dry_running": "dry_run", "awaiting_approval": "approval", "approved": "live"}.get(p["status"]) if p else None
        return {"next": nxt, "proposal_id": str(p["id"])} if nxt else END

    def _history(self, conn, tenant_id: str, skill_id: str) -> dict[str, int]:
        row = conn.execute(
            "SELECT count(*) FILTER (WHERE status IN ('done', 'failed') AND evidence IS NOT NULL) AS live_runs_30d, "
            "count(*) FILTER (WHERE status = 'failed') AS failures_30d FROM proposals "
            "WHERE tenant_id = %s AND skill_id = %s AND created_at > now() - interval '30 days'",
            (tenant_id, skill_id),
        ).fetchone()
        return {"live_runs_30d": row["live_runs_30d"], "failures_30d": row["failures_30d"]}

    def _propose(self, conn, tenant: Tenant, case: dict[str, Any], finding: investigator.Finding) -> Step:
        case_id = str(case["id"])
        if not finding.skill_id:
            return self._escalate(conn, tenant.id, case_id, "no skill answers this case", finding.engineer_should_check)
        try:
            skill = self.skills.get(finding.skill_id)
        except KeyError as exc:
            return self._escalate(conn, tenant.id, case_id, str(exc), finding.engineer_should_check)
        asset_id = f"{skill.target_kind}:{finding.target}"
        try:
            known = self.graph.asset(tenant.id, asset_id) is not None
        except Exception:
            known = False
        if not finding.target or not known:
            return self._escalate(
                conn,
                tenant.id,
                case_id,
                f"{finding.target} is not a known {skill.target_kind} of {tenant.name}",
                finding.engineer_should_check,
            )
        params, errors = self.skills.validate_params(skill.id, finding.params)
        if errors:
            return self._escalate(
                conn, tenant.id, case_id, "proposal failed validation: " + "; ".join(errors), finding.engineer_should_check
            )

        try:
            radius = self.graph.blast_radius(tenant.id, [asset_id])
        except Exception:
            radius = {"dependents": 0, "services": [], "critical": True, "users": 0}  # unknown counts as critical
        decision = self.policy.decide(
            build_input(tenant, skill, [finding.target], radius, self._history(conn, tenant.id, skill.id))
        )
        policy_body = {"mode": decision.mode, "reason": decision.reason, "wanted": decision.wanted, "caps": decision.caps}
        proposal = conn.execute(
            "INSERT INTO proposals (tenant_id, case_id, skill_id, skill_version, library_version, targets, params, risk, mode, "
            "policy, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                tenant.id,
                case_id,
                skill.id,
                skill.version,
                self.skills.version,
                finding.target,
                _json(params),
                skill.risk,
                decision.mode,
                _json(policy_body),
                "dry_running",
            ),
        ).fetchone()
        if radius.get("services"):
            conn.execute("UPDATE cases SET service = coalesce(service, %s) WHERE id = %s", (radius["services"][0], case_id))
        ledger.append(
            conn,
            tenant.id,
            case_id,
            "agent:remediator",
            "proposal",
            {
                "proposal_id": str(proposal["id"]),
                "skill_id": skill.id,
                "skill_title": skill.title,
                "skill_version": skill.version,
                "target": finding.target,
                "params": params,
                "risk": skill.risk,
                "approval": skill.approval,
                "mode": decision.mode,
                "policy": policy_body,
                "blast_radius": radius,
                "rollback": skill.rollback,
            },
        )
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
            if case and case["status"] not in ("resolved", "escalated", "merged"):
                self._escalate(conn, tenant_id, case_id, reason)
        return END

    # ---- runs ---------------------------------------------------------------------------------------------

    def run_skill(self, tenant_id: str, proposal_id: str, live: bool) -> Step:
        """The dry run (gather and decide) or the live run (gather, decide, act, verify) of a proposal's skill."""
        tenant = self.tenants.get(tenant_id)
        with self.db.scope(tenant_id) as conn:
            p = conn.execute("SELECT * FROM proposals WHERE id = %s FOR UPDATE", (proposal_id,)).fetchone()
            if p is None:
                raise LookupError("no such proposal")
            expected = "approved" if live else "dry_running"
            if p["status"] == "running" and live:
                # A worker died mid-run. The skill re-gathers and re-decides, so running it again is safe.
                pass
            elif p["status"] != expected:
                return self._resume(conn, str(p["case_id"]))
            case = conn.execute("SELECT ref FROM cases WHERE id = %s", (p["case_id"],)).fetchone()
            if live:
                conn.execute("UPDATE proposals SET status = 'running' WHERE id = %s", (proposal_id,))
        skill = self.skills.get(p["skill_id"])
        ctx = Context(
            tenant=tenant,
            target=p["targets"],
            params=p["params"],
            skill=skill,
            _connectors=self.connectors(tenant),
            graph=self.graph,
            dry_run=not live,
            approval_ref=f"{case['ref']}/{proposal_id}" if live and p["decided_by"] else None,
        )
        evidence = run_skill_module(self.skills, ctx, live)
        with self.db.scope(tenant_id) as conn:
            case_id = str(p["case_id"])
            actor = f"skill:{skill.id}"
            ledger.append(
                conn,
                tenant_id,
                case_id,
                actor,
                "run_result",
                {"proposal_id": proposal_id, "dry_run": not live, "evidence": evidence},
            )
            if not live:
                return self._after_dry_run(conn, tenant_id, p, evidence)
            return self._after_live_run(conn, tenant_id, p, evidence)

    def _after_dry_run(self, conn, tenant_id: str, p: dict[str, Any], evidence: dict[str, Any]) -> Step:
        case_id = str(p["case_id"])
        decision = evidence.get("decision") or {}
        action, reason = decision.get("action"), decision.get("reason", "")
        conn.execute("UPDATE proposals SET dry_run_evidence = %s WHERE id = %s", (_json(evidence), p["id"]))
        if evidence["outcome"] == "failed":
            conn.execute("UPDATE proposals SET status = 'failed', outcome = 'failed' WHERE id = %s", (p["id"],))
            return self._escalate(conn, tenant_id, case_id, f"the dry run failed: {evidence.get('error')}")
        if action == "none":
            conn.execute("UPDATE proposals SET status = 'done', outcome = 'no_action' WHERE id = %s", (p["id"],))
            conn.execute(
                "UPDATE cases SET status = 'resolved', resolved_at = now(), updated_at = now() WHERE id = %s", (case_id,)
            )
            ledger.append(conn, tenant_id, case_id, "agent:verifier", "resolved", {"reason": f"nothing to do: {reason}"})
            return END
        if action == "escalate":
            conn.execute("UPDATE proposals SET status = 'done', outcome = 'escalated' WHERE id = %s", (p["id"],))
            return self._escalate(conn, tenant_id, case_id, f"the skill's own checks escalated: {reason}")
        if p["mode"] == "recommend":
            conn.execute("UPDATE proposals SET status = 'proposed' WHERE id = %s", (p["id"],))
            return self._escalate(
                conn,
                tenant_id,
                case_id,
                f"policy allows this skill only as a recommendation here: {p['policy'].get('reason', '')}",
                f"Run the dry run's plan by hand if you agree: {reason}",
            )
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
            {"proposal_id": str(p["id"]), "action": action, "reason": reason, "plan": decision.get("plan", [])},
        )
        return {"next": "approval", "proposal_id": str(p["id"])}

    def _after_live_run(self, conn, tenant_id: str, p: dict[str, Any], evidence: dict[str, Any]) -> Step:
        case_id = str(p["case_id"])
        outcome = evidence["outcome"]
        status = {"fixed": "done", "no_action": "done", "escalated": "done"}.get(outcome, "failed")
        conn.execute(
            "UPDATE proposals SET status = %s, outcome = %s, evidence = %s WHERE id = %s",
            (status, outcome, _json(evidence), p["id"]),
        )
        if outcome == "fixed":
            ledger.append(
                conn,
                tenant_id,
                case_id,
                "agent:verifier",
                "verified",
                {"proposal_id": str(p["id"]), "after": evidence.get("after")},
            )
            conn.execute(
                "UPDATE cases SET status = 'resolved', resolved_at = now(), updated_at = now() WHERE id = %s", (case_id,)
            )
            reason = (evidence.get("decision") or {}).get("reason", "")
            ledger.append(
                conn,
                tenant_id,
                case_id,
                "agent:verifier",
                "resolved",
                {"reason": f"{p['skill_id']} fixed it and the check passed. {reason}"},
            )
        elif outcome == "no_action":
            conn.execute(
                "UPDATE cases SET status = 'resolved', resolved_at = now(), updated_at = now() WHERE id = %s", (case_id,)
            )
            ledger.append(
                conn,
                tenant_id,
                case_id,
                "agent:verifier",
                "resolved",
                {"reason": (evidence.get("decision") or {}).get("reason", "")},
            )
        else:
            reason = (evidence.get("decision") or {}).get("reason") if outcome == "escalated" else evidence.get("error")
            self._escalate(
                conn,
                tenant_id,
                case_id,
                f"live run {outcome}: {reason}",
                p["skill_id"] and self.skills.get(p["skill_id"]).rollback,
            )
        return END

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
                "UPDATE proposals SET status = %s, decided_by = %s, decided_at = now() WHERE id = %s", (status, who, proposal_id)
            )
            ledger.append(conn, tenant_id, p["case_id"], f"engineer:{who}", status, {"proposal_id": proposal_id, "note": note})
            if approve:
                conn.execute("UPDATE cases SET status = 'running', updated_at = now() WHERE id = %s", (p["case_id"],))
            else:
                self._escalate(conn, tenant_id, str(p["case_id"]), f"{who} rejected the skill" + (f": {note}" if note else ""))
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
