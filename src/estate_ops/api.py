"""HTTP API: alert intake, and the cockpit's case, approval, estate, skills and lab endpoints.

Intake checks each alert and queues it on NATS, so it answers fast and keeps working while Postgres or
the worker is busy. Approvals go to the case's Temporal workflow as a "decide" update, which refuses a
proposal that is not waiting for a decision; the API never writes a decision itself.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from temporalio.client import WorkflowUpdateFailedError
from temporalio.service import RPCError

from . import __version__, auth, budget, ledger, sources, triage, views
from .bus import Bus
from .service import Platform
from .workflows import CaseWorkflow, DecideInput, workflow_id

COCKPIT_DIR = Path(os.environ.get("COCKPIT_DIR") or Path(__file__).resolve().parents[2] / "cockpit" / "dist")


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    who: str = Field(default="", max_length=80)


class Decision(BaseModel):
    who: str = Field(default="", max_length=80)  # only used with the shared token; SSO uses the ID token
    note: str = Field(default="", max_length=500)


def _uuid(value: str, what: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise HTTPException(404, f"no such {what}") from None


class RateLimit:
    """At most `per_minute` calls per key in any minute, in this process. Enough to stop a stuck browser
    tab from spending a customer's AI budget."""

    def __init__(self, per_minute: int):
        self.per_minute, self.calls = per_minute, {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self.calls.get(key, []) if now - t < 60]
        if len(recent) >= self.per_minute:
            self.calls[key] = recent
            return False
        self.calls[key] = [*recent, now]
        return True


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def create_app(platform: Platform) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from .worker import temporal_client

        s = platform.settings
        app.state.bus = await Bus.connect(s.nats_url, s.nats_prefix)
        await app.state.bus.ensure_streams()
        app.state.temporal = await temporal_client(platform)
        yield
        await app.state.bus.close()

    app = FastAPI(title="Estate Ops Platform", version=__version__, lifespan=lifespan)
    app.state.platform = platform
    auth.install(app, platform.settings)
    limiter = RateLimit(per_minute=12)

    @app.get("/auth/me", include_in_schema=False)
    def me(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        user = auth.current_user(request, platform.settings, authorization)
        return {
            "sso": auth.sso_enabled(platform.settings),
            "user": user if user and user["sub"] != "console-token" else None,
            "signed_in": user is not None,
        }

    @app.post("/auth/logout", include_in_schema=False)
    def logout(request: Request) -> dict[str, Any]:
        request.session.clear()
        return {"ok": True}

    def console_auth(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        user = auth.current_user(request, platform.settings, authorization)
        if user is None:
            raise HTTPException(401, "sign in required")
        return user

    def engineer_scope() -> list[str]:
        return platform.tenants.ids()  # later: the tenants this engineer's team serves

    def scoped(tenant: str | None) -> list[str]:
        """One customer when asked for (and theirs to see), else every customer in scope."""
        if tenant is None:
            return engineer_scope()
        if tenant not in engineer_scope():
            raise HTTPException(403, "not your customer")
        return [tenant]

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "skills": len(platform.skills.ids()),
            "library_version": platform.skills.version,
            "model": platform.settings.diagnosis_model if platform.anthropic else None,
        }

    @app.post("/api/v1/intake/alertmanager/api/v2/alerts", status_code=200)
    async def alertmanager(request: Request, authorization: str | None = Header(default=None)):
        """vmalert's notifier: Alertmanager API v2 alerts for every customer; each alert's tenant_id label says whose."""
        expected = os.environ.get(platform.settings.alerts_token_env) or ""
        presented = _bearer(authorization) or ""
        if not expected or not hmac.compare_digest(expected.encode(), presented.encode()):
            raise HTTPException(401, "bad alerts token")
        try:
            raw = await request.json()
            if not isinstance(raw, list):
                raise ValueError("expected a list of alerts")
            items = sources.alertmanager(raw)
            for tenant_id, _, payload in items:
                platform.tenants.get(tenant_id)
                triage.check(tenant_id, payload)
        except (triage.IntakeError, KeyError, TypeError, ValueError) as exc:
            raise HTTPException(422, f"not an Alertmanager alert list: {exc}") from None
        for tenant_id, intake_id, payload in items:
            await app.state.bus.publish_intake(tenant_id, intake_id, payload)
        return {"queued": len(items)}

    @app.post("/api/v1/intake/{source}/{tenant_id}", status_code=202)
    async def intake(source: str, tenant_id: str, request: Request, authorization: str | None = Header(default=None)):
        """Other alert webhooks, per customer with its intake token, in the platform's alert shape."""
        normalize = sources.WEBHOOKS.get(source)
        if normalize is None:
            raise HTTPException(404, "unknown alert source")
        try:
            tenant = platform.tenants.get(tenant_id)
        except KeyError:
            raise HTTPException(404, "unknown tenant") from None
        if not tenant.intake_token_ok(_bearer(authorization)):
            raise HTTPException(401, "bad intake token")
        try:
            raw = await request.json()
        except ValueError:
            raise HTTPException(422, "expected a JSON object") from None
        if not isinstance(raw, dict):
            raise HTTPException(422, "expected a JSON object")
        try:
            payloads = normalize(tenant, raw)
            for payload in payloads:
                triage.check(tenant_id, payload)
        except (triage.IntakeError, KeyError, TypeError, ValueError) as exc:
            raise HTTPException(422, f"not a {source} alert: {exc}") from None
        ids = []
        for payload in payloads:
            intake_id = str(uuid.uuid4())
            await app.state.bus.publish_intake(tenant_id, intake_id, payload)
            ids.append(intake_id)
        return {"intake_id": ids[0] if len(ids) == 1 else None, "intake_ids": ids}

    @app.get("/api/v1/cases", dependencies=[Depends(console_auth)])
    def list_cases(
        status: str | None = None, tenant: str | None = None, source: str | None = None, q: str | None = None
    ) -> list[dict[str, Any]]:
        return views.cases(platform, scoped(tenant), status, source, q)

    @app.get("/api/v1/cases/{case_id}", dependencies=[Depends(console_auth)])
    def get_case(case_id: str) -> dict[str, Any]:
        detail = views.case_detail(platform, engineer_scope(), _uuid(case_id, "case"))
        if detail is None:
            raise HTTPException(404, "no such case")
        return detail

    @app.post("/api/v1/cases/{case_id}/ask")
    def ask(case_id: str, body: Question, user: dict = Depends(console_auth)) -> dict[str, Any]:  # noqa: B008
        who = auth.signature(user, body.who)
        if not who:
            raise HTTPException(422, "a question needs a named engineer")
        if not limiter.allow(user.get("sub", "") + "|" + who):
            raise HTTPException(429, "too many questions; try again in a minute")
        answer = views.ask_copilot(platform, engineer_scope(), _uuid(case_id, "case"), body.question.strip(), who)
        if answer is None:
            raise HTTPException(404, "no such case")
        return answer

    @app.get("/api/v1/overview", dependencies=[Depends(console_auth)])
    def overview(tenant: str | None = None) -> dict[str, Any]:
        return views.overview(platform, scoped(tenant))

    @app.get("/api/v1/estate/{tenant_id}", dependencies=[Depends(console_auth)])
    def estate(tenant_id: str, q: str = "", kind: str | None = None) -> dict[str, Any]:
        return views.estate(platform, scoped(tenant_id)[0], q, kind)

    @app.get("/api/v1/estate/{tenant_id}/assets/{asset_id:path}", dependencies=[Depends(console_auth)])
    def asset(tenant_id: str, asset_id: str, hops: int = 2) -> dict[str, Any]:
        detail = views.asset_detail(platform, scoped(tenant_id)[0], asset_id, hops)
        if detail is None:
            raise HTTPException(404, "no such asset")
        return detail

    @app.get("/api/v1/skills", dependencies=[Depends(console_auth)])
    def skills(tenant: str | None = None) -> dict[str, Any]:
        return {"library_version": platform.skills.version, "skills": views.skills(platform, scoped(tenant))}

    @app.get("/api/v1/integrations", dependencies=[Depends(console_auth)])
    def integration_status(tenant: str | None = None) -> list[dict[str, Any]]:
        return views.integrations(platform, scoped(tenant), sources.configured(platform))

    @app.get("/api/v1/cost", dependencies=[Depends(console_auth)])
    def cost(tenant: str | None = None, days: int = 30) -> dict[str, Any]:
        return views.cost(platform, scoped(tenant), days)

    async def _decide(proposal_id: str, body: Decision, approve: bool, user: dict[str, Any]) -> dict[str, Any]:
        try:
            uuid.UUID(proposal_id)
        except ValueError:
            raise HTTPException(404, "no such proposal") from None
        who = auth.signature(user, body.who)
        if not who:
            raise HTTPException(422, "a decision needs a named engineer")

        def lookup():
            with platform.db.scope(engineer_scope()) as conn:
                return conn.execute("SELECT case_id, status FROM proposals WHERE id = %s", (proposal_id,)).fetchone()

        row = await asyncio.to_thread(lookup)
        if row is None:
            raise HTTPException(404, "no such proposal")
        handle = app.state.temporal.get_workflow_handle(workflow_id(str(row["case_id"])))
        for _ in range(30):
            try:
                result = await handle.execute_update(CaseWorkflow.decide, DecideInput(proposal_id, approve, who, body.note))
                return {"proposal_id": result["proposal_id"], "status": result["status"]}
            except WorkflowUpdateFailedError as exc:
                reason = getattr(exc.cause, "message", None) or "this proposal cannot be decided"
            except RPCError:
                reason = "this case is no longer waiting for a decision"
            # The proposal can be marked awaiting a moment before its workflow starts waiting; retry briefly.
            row = await asyncio.to_thread(lookup)
            if row["status"] != "awaiting_approval":
                break
            await asyncio.sleep(0.1)
        raise HTTPException(409, reason)

    @app.post("/api/v1/proposals/{proposal_id}/approve")
    async def approve(proposal_id: str, body: Decision, user: dict = Depends(console_auth)) -> dict[str, Any]:  # noqa: B008
        return await _decide(proposal_id, body, True, user)

    @app.post("/api/v1/proposals/{proposal_id}/reject")
    async def reject(proposal_id: str, body: Decision, user: dict = Depends(console_auth)) -> dict[str, Any]:  # noqa: B008
        return await _decide(proposal_id, body, False, user)

    @app.get("/api/v1/tenants", dependencies=[Depends(console_auth)])
    def tenants() -> list[dict[str, Any]]:
        out = []
        for t in platform.tenants:
            with platform.db.scope(t.id) as conn:
                spend = budget.month_spend(conn, t.id)
                ok, broken_at = ledger.verify(conn, t.id)
            out.append(
                {
                    "id": t.id,
                    "name": t.name,
                    "industry": t.industry,
                    "autonomy": t.autonomy,
                    "diagnosis": t.diagnosis,
                    "ai_spend_month_usd": float(spend),
                    "ai_budget_usd_month": t.ai_budget_usd_month,
                    "ledger_ok": ok,
                    "ledger_broken_at": broken_at,
                }
            )
        return out

    # ---- lab: the estate simulator, when one is attached ----------------------------------------------------

    async def _sim(method: str, path: str) -> Any:
        if not platform.settings.sim_url:
            raise HTTPException(404, "no simulator is attached")
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.request(method, f"{platform.settings.sim_url.rstrip('/')}/_sim/{path}")
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.json().get("detail", "simulator refused"))
        return r.json()

    @app.get("/api/v1/lab/faults", dependencies=[Depends(console_auth)])
    async def lab_faults() -> Any:
        return await _sim("GET", "faults")

    @app.post("/api/v1/lab/faults/{fault}", dependencies=[Depends(console_auth)])
    async def lab_inject(fault: str) -> Any:
        return await _sim("POST", f"faults/{fault}")

    @app.post("/api/v1/lab/reset", dependencies=[Depends(console_auth)])
    async def lab_reset() -> Any:
        return await _sim("POST", "reset")

    @app.get("/api/v1/lab/events", dependencies=[Depends(console_auth)])
    async def lab_events() -> Any:
        return await _sim("GET", "events")

    # ---- the cockpit (a built single-page app) -------------------------------------------------------------

    if COCKPIT_DIR.is_dir():
        if (COCKPIT_DIR / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=COCKPIT_DIR / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def cockpit(path: str):
            if path.startswith(("api/", "auth/")):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            file = (COCKPIT_DIR / path).resolve()
            if path and file.is_file() and file.is_relative_to(COCKPIT_DIR.resolve()):
                return FileResponse(file)
            return FileResponse(COCKPIT_DIR / "index.html")

    return app
