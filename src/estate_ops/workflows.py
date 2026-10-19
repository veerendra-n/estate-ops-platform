"""The case workflow on Temporal: settle, investigate, dry run, wait for an engineer, run and verify.

    alerts -> case (triage) -> settle -> investigate -> proposal -> dry run -> approval -> live run + verify -> resolved
                                                    `-> escalated (no skill, invalid proposal, the skill escalates,
                                                        recommend-only policy, rejection, no decision in time,
                                                        a failed check, a step that keeps failing)

Temporal holds the waiting (the settle window, an approval that takes a day, a long backup rerun) and retries
a step that fails, so a worker restart never loses a case. The steps are Platform methods in service.py run
as activities; they write the case, the proposal and the ledger in Postgres, and this module only decides
what runs next. It runs in
Temporal's workflow sandbox, so it imports nothing but the standard library and temporalio, and calls
activities by name.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

STEP_RETRY = RetryPolicy(maximum_attempts=5, non_retryable_error_types=["WorkflowError", "LookupError"])
# A live run re-gathers and re-decides before acting, so a retry is safe, but don't hammer an estate.
RUN_RETRY = RetryPolicy(maximum_attempts=2, non_retryable_error_types=["WorkflowError", "LookupError"])


@dataclass
class CaseInput:
    tenant_id: str
    case_id: str
    settle_seconds: float
    approval_timeout_seconds: float


@dataclass
class DecideInput:
    proposal_id: str
    approve: bool
    who: str
    note: str = ""


def workflow_id(case_id: str) -> str:
    return f"case-{case_id}"


@workflow.defn(name="CaseWorkflow")
class CaseWorkflow:
    def __init__(self) -> None:
        self.input: CaseInput | None = None
        self.awaiting: str | None = None  # proposal id waiting on an engineer
        self.deciding = False
        self.decided: dict[str, Any] | None = None

    async def _step(self, name: str, *args: Any, minutes: float = 2, retry: RetryPolicy = STEP_RETRY) -> Any:
        return await workflow.execute_activity(
            name, args=list(args), start_to_close_timeout=timedelta(minutes=minutes), retry_policy=retry
        )

    @workflow.run
    async def run(self, inp: CaseInput) -> str:
        self.input = inp
        t = inp.tenant_id
        stage = "investigation"
        try:
            # Symptoms of one problem arrive over a minute or so; let triage fold them in before investigating.
            if inp.settle_seconds > 0:
                await workflow.sleep(timedelta(seconds=inp.settle_seconds))
            step = await self._step("investigate", t, inp.case_id, minutes=5)
            while step["next"] != "end":
                pid = step["proposal_id"]
                if step["next"] == "dry_run":
                    stage = "dry run"
                    step = await self._step("run_skill", t, pid, False, minutes=5)
                elif step["next"] == "live":
                    stage = "live run"
                    step = await self._step("run_skill", t, pid, True, minutes=60, retry=RUN_RETRY)
                elif step["next"] == "approval":
                    stage = "approval"
                    step = await self._await_decision(pid)
                else:
                    raise ApplicationError(f"unknown step {step['next']}", non_retryable=True)
        except ActivityError as exc:
            cause = exc.cause.message if isinstance(exc.cause, ApplicationError) else str(exc.cause or exc)
            await self._step("escalate", t, inp.case_id, f"the {stage} step kept failing: {cause}")
            return "escalated"
        await workflow.wait_condition(workflow.all_handlers_finished)
        return "done"

    async def _await_decision(self, proposal_id: str) -> dict[str, Any]:
        assert self.input is not None
        self.awaiting, self.decided = proposal_id, None
        try:
            await workflow.wait_condition(
                lambda: self.decided is not None, timeout=timedelta(seconds=self.input.approval_timeout_seconds)
            )
        except TimeoutError:
            if not self.deciding:
                self.awaiting = None
                return await self._step("expire_approval", self.input.tenant_id, proposal_id, self.input.approval_timeout_seconds)
            await workflow.wait_condition(lambda: self.decided is not None or not self.deciding)
            if self.decided is None:
                return await self._step("expire_approval", self.input.tenant_id, proposal_id, self.input.approval_timeout_seconds)
        decided, self.decided, self.awaiting = self.decided, None, None
        return decided

    @workflow.update
    async def decide(self, d: DecideInput) -> dict[str, Any]:
        # Two approvals can pass the validator in the same task; only the first one gets past here.
        if self.deciding or self.awaiting != d.proposal_id:
            raise ApplicationError("this proposal is not awaiting a decision", non_retryable=True)
        assert self.input is not None
        self.deciding = True
        try:
            result = await self._step("decide", self.input.tenant_id, d.proposal_id, d.approve, d.who, d.note)
        finally:
            self.deciding = False
        self.decided = result
        return result

    @decide.validator
    def _check_decide(self, d: DecideInput) -> None:
        if not d.who.strip():
            raise ValueError("a decision needs a named engineer")
        if self.deciding or self.awaiting != d.proposal_id:
            raise ValueError("this proposal is not awaiting a decision")

    @workflow.query
    def awaiting_proposal(self) -> str | None:
        return self.awaiting
