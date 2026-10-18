"""Console sign-in with OpenID Connect: Microsoft Entra ID, Google Workspace, or any OIDC provider.

Authorization code flow with PKCE and a nonce, through Authlib, which reads the provider's discovery
document and checks the ID token's signature, issuer, audience, expiry and nonce. The signed-in engineer
lives in a signed session cookie; approvals are recorded under the name and email from the ID token,
not under a name typed in the browser.

OIDC_ALLOWED, when set, also limits who may sign in by the Entra tenant id (tid) or the Google Workspace
domain (hd) in the token. With an Entra issuer for one tenant, or a Google app marked internal, the
provider already does that.

Without OIDC_ISSUER the console uses the shared CONSOLE_TOKEN, which is for a laptop or a lab only.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from .config import Settings


def sso_enabled(settings: Settings) -> bool:
    return bool(settings.oidc_issuer and settings.oidc_client_id)


def install(app: FastAPI, settings: Settings) -> None:
    """Add the session cookie and, when an issuer is configured, the /auth routes."""
    secret = settings.session_secret
    if sso_enabled(settings) and not secret:
        raise RuntimeError("SESSION_SECRET is required when OIDC_ISSUER is set")
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret or os.urandom(32).hex(),
        session_cookie="eo_session",
        max_age=12 * 3600,
        same_site="lax",
        https_only=settings.public_url.startswith("https://"),
    )
    if not sso_enabled(settings):
        return

    from authlib.integrations.starlette_client import OAuth, OAuthError

    oauth = OAuth()
    oauth.register(
        "oidc",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        server_metadata_url=settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email", "code_challenge_method": "S256"},
    )

    @app.get("/auth/login", include_in_schema=False)
    async def login(request: Request):
        return await oauth.oidc.authorize_redirect(request, f"{settings.public_url}/auth/callback")

    @app.get("/auth/callback", include_in_schema=False)
    async def callback(request: Request):
        try:
            token = await oauth.oidc.authorize_access_token(request)
        except OAuthError as exc:
            raise HTTPException(401, f"sign-in failed: {exc.error}") from None
        claims = token.get("userinfo") or {}
        if not allowed(claims, settings.oidc_allowed):
            raise HTTPException(403, "this account may not use the console")
        request.session["user"] = {
            "sub": claims["sub"],
            "iss": claims.get("iss"),
            "name": claims.get("name") or claims.get("preferred_username") or claims.get("email") or claims["sub"],
            "email": claims.get("email") or claims.get("preferred_username"),
        }
        return RedirectResponse("/", status_code=303)


def allowed(claims: dict[str, Any], allow: tuple[str, ...]) -> bool:
    if claims.get("email") and claims.get("email_verified") is False:
        return False
    if not allow:
        return True
    return any(str(claims.get(k, "")).lower() in allow for k in ("tid", "hd"))


def current_user(request: Request, settings: Settings, authorization: str | None) -> dict[str, Any] | None:
    """The signed-in engineer, or a token user when SSO is off; None when neither."""
    user = request.session.get("user") if "session" in request.scope else None
    if user:
        return user
    if sso_enabled(settings):
        return None  # the shared token is not accepted once single sign-on is on
    expected = os.environ.get("CONSOLE_TOKEN")
    presented = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else None
    if expected and presented and hmac.compare_digest(expected.encode(), presented.encode()):
        return {"sub": "console-token", "name": None, "email": None}
    return None


def signature(user: dict[str, Any], typed_name: str) -> str:
    """The name an approval is recorded under: the ID token's, or the typed one in token mode."""
    if user.get("sub") == "console-token":
        return typed_name.strip()
    label = user["name"] + (f" <{user['email']}>" if user.get("email") and user["email"] != user["name"] else "")
    return label[:80]
