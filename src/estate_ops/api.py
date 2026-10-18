"""HTTP API: alert intake for OpenObserve, case and approval endpoints for the console."""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import budget, ledger, triage
from .service import Platform, WorkflowError

CONSOLE_DIR = Path(__file__).resolve().parents[2] / "console"


class Decision(BaseModel):
    who: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=500)


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def create_app(platform: Platform) -> FastAPI:
    app = FastAPI(title="Estate Ops Platform", version="0.1.0")
    app.state.platform = platform

    def console_auth(authorization: str | None = Header(default=None)) -> None:
        # One shared console token until SSO (OIDC) is added; engineers still sign approvals by name.
        expected = os.environ.get("CONSOLE_TOKEN")
        presented = _bearer(authorization)
        if not expected or not presented or not hmac.compare_digest(expected.encode(), presented.encode()):
            raise HTTPException(401, "console token required")

    def engineer_scope() -> list[str]:
        return platform.tenants.ids()  # later: the tenants this engineer's team serves

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "catalog_commit": platform.catalog.commit,
            "runbooks": len(platform.catalog.runbook_ids()),
            "model": platform.settings.diagnosis_model if platform.anthropic else None,
        }

    @app.post("/api/v1/intake/openobserve/{tenant_id}", status_code=202)
    async def intake(tenant_id: str, request: Request, authorization: str | None = Header(default=None)):
        try:
            tenant = platform.tenants.get(tenant_id)
        except KeyError:
            raise HTTPException(404, "unknown tenant") from None
        if not tenant.intake_token_ok(_bearer(authorization)):
            raise HTTPException(401, "bad intake token")
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(422, "expected a JSON object")
        try:
            with platform.db.scope(tenant_id) as conn:
                results = triage.ingest(conn, tenant_id, platform.catalog, payload)
        except triage.IntakeError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"cases": [{"case_id": r.case_id, "ref": r.ref, "new": r.new, "target": r.target} for r in results]}

    @app.get("/api/v1/cases", dependencies=[Depends(console_auth)])
    def list_cases(status: str | None = None, tenant: str | None = None) -> list[dict[str, Any]]:
        scope = [tenant] if tenant else engineer_scope()
        if tenant and tenant not in engineer_scope():
            raise HTTPException(403, "not your customer")
        with platform.db.scope(scope) as conn:
            return conn.execute(
                "SELECT c.id, c.tenant_id, t.name AS tenant_name, c.ref, c.title, c.severity, c.status, c.target, "
                "c.summary, c.opened_at, c.updated_at, (SELECT count(*) FROM signals s WHERE s.case_id = c.id) AS signals "
                "FROM cases c JOIN tenants t ON t.id = c.tenant_id "
                "WHERE (%(status)s::text IS NULL OR c.status = %(status)s) ORDER BY c.updated_at DESC LIMIT 200",
                {"status": status},
            ).fetchall()

    @app.get("/api/v1/cases/{case_id}", dependencies=[Depends(console_auth)])
    def get_case(case_id: str) -> dict[str, Any]:
        with platform.db.scope(engineer_scope()) as conn:
            case = conn.execute(
                "SELECT c.*, t.name AS tenant_name FROM cases c JOIN tenants t ON t.id = c.tenant_id WHERE c.id = %s",
                (case_id,),
            ).fetchone()
            if case is None:
                raise HTTPException(404, "no such case")
            return {
                "case": case,
                "signals": conn.execute(
                    "SELECT alert_id, target, labels, received_at FROM signals WHERE case_id = %s ORDER BY id", (case_id,)
                ).fetchall(),
                "proposals": conn.execute(
                    "SELECT * FROM proposals WHERE case_id = %s ORDER BY created_at", (case_id,)
                ).fetchall(),
                "timeline": ledger.for_case(conn, case_id),
                "ai_cost_usd": float(
                    conn.execute(
                        "SELECT coalesce(sum(cost_usd), 0) AS c FROM ai_usage WHERE case_id = %s", (case_id,)
                    ).fetchone()["c"]
                ),
            }

    def _decide(proposal_id: str, body: Decision, approve: bool) -> dict[str, Any]:
        with platform.db.scope(engineer_scope()) as conn:
            row = conn.execute("SELECT tenant_id FROM proposals WHERE id = %s", (proposal_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such proposal")
        try:
            return platform.decide(row["tenant_id"], proposal_id, approve, body.who, body.note)
        except WorkflowError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/v1/proposals/{proposal_id}/approve", dependencies=[Depends(console_auth)])
    def approve(proposal_id: str, body: Decision) -> dict[str, Any]:
        return _decide(proposal_id, body, True)

    @app.post("/api/v1/proposals/{proposal_id}/reject", dependencies=[Depends(console_auth)])
    def reject(proposal_id: str, body: Decision) -> dict[str, Any]:
        return _decide(proposal_id, body, False)

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
                    "diagnosis": t.diagnosis,
                    "ai_spend_month_usd": float(spend),
                    "ai_budget_usd_month": t.ai_budget_usd_month,
                    "ledger_ok": ok,
                    "ledger_broken_at": broken_at,
                }
            )
        return out

    if CONSOLE_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=CONSOLE_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def console() -> FileResponse:
            return FileResponse(CONSOLE_DIR / "index.html")

    return app
