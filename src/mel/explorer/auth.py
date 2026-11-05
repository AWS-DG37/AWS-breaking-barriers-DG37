"""Cognito sign-in for the hosted console, handled on the server.

The browser only ever holds cookies on the console's own origin: a short-lived
sign-in cookie carrying the PKCE state, and an HttpOnly, SameSite=Strict session
cookie carrying the Cognito access token. Every /api request outside /api/auth is
checked against Cognito's signing keys or the optional signed viewer session. Viewers
can only read saved data. Without auth settings, as in local development, the console
is open and /api/auth/session says so.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from collections import deque
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import Field, SecretStr

from mel.config import Settings, StrictModel

SESSION_COOKIE = "mel_session"
SIGN_IN_COOKIE = "mel_sign_in"
OPEN_PATHS = frozenset(
    {
        "/api/auth/login",
        "/api/auth/callback",
        "/api/auth/logout",
        "/api/auth/options",
        "/api/auth/viewer",
    }
)
# GET routes that spend money. View-only sessions cannot reach them.
PAID_GETS = frozenset({"/api/agents/options", "/api/voice"})
SIGN_IN_SECONDS = 600
VIEWER_SECONDS = 8 * 60 * 60


class ViewerLogin(StrictModel):
    password: SecretStr = Field(max_length=128)


class Unauthorized(Exception):
    pass


class TokenVerifier:
    """Accepts only unexpired Cognito access tokens issued to this console's client."""

    def __init__(self, issuer: str, client_id: str, keys=None):
        self.issuer = issuer
        self.client_id = client_id
        self.keys = keys or jwt.PyJWKClient(f"{issuer}/.well-known/jwks.json", cache_keys=True)

    def claims(self, token: str) -> dict:
        try:
            key = self.keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.issuer,
                leeway=30,
                options={"require": ["exp", "iss", "token_use", "client_id"]},
            )
        except jwt.PyJWTError as error:
            raise Unauthorized(type(error).__name__) from None
        if claims["token_use"] != "access" or claims["client_id"] != self.client_id:
            raise Unauthorized("token was not issued to this console")
        return claims


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def install(app: FastAPI, settings: Settings, *, verifier=None, exchange=None) -> None:
    """Add /api/auth routes and, when sign-in is configured, the session check."""
    if settings.auth_domain is None:

        @app.get("/api/auth/options")
        def open_options():
            return {"auth": False, "viewer": False}

        @app.get("/api/auth/session")
        def open_session():
            return {"auth": False}

        return

    verifier = verifier or TokenVerifier(settings.auth_issuer, settings.auth_client_id)
    exchange = exchange or _exchange(settings)
    callback = f"{settings.console_url}/api/auth/callback"
    password = settings.viewer_password.get_secret_value() if settings.viewer_password else None
    viewer_key = hashlib.sha256(f"mel-viewer-session:{password}".encode()).digest()
    # ponytail: five failures/minute per process, shared across clients. At multi-instance
    # scale, move this bound to the shared reverse proxy or a distributed rate limiter.
    failures: deque[float] = deque(maxlen=5)

    @app.get("/api/auth/options")
    def options():
        return {"auth": True, "viewer": password is not None}

    @app.post("/api/auth/viewer")
    async def viewer_login(value: ViewerLogin):
        if password is None:
            raise HTTPException(404, "Viewer login is not enabled")
        now = time.monotonic()
        while failures and failures[0] <= now - 60:
            failures.popleft()
        if len(failures) == 5:
            raise HTTPException(
                429,
                "Too many sign-in attempts. Try again shortly.",
                headers={"Retry-After": str(max(1, int(61 - now + failures[0])))},
            )
        supplied = value.password.get_secret_value().encode()
        if not secrets.compare_digest(supplied, password.encode()):
            failures.append(now)
            raise HTTPException(401, "Incorrect viewer password")
        token = jwt.encode(
            {
                "sub": "Viewer",
                "role": "viewer",
                "aud": settings.console_url,
                "iss": "mel-viewer",
                "exp": int(time.time()) + VIEWER_SECONDS,
            },
            viewer_key,
            algorithm="HS256",
        )
        response = JSONResponse({"auth": True, "user": "Viewer", "read_only": True})
        response.set_cookie(
            SESSION_COOKIE,
            f"viewer.{token}",
            max_age=VIEWER_SECONDS,
            path="/api",
            httponly=True,
            secure=True,
            samesite="strict",
        )
        return response

    @app.middleware("http")
    async def require_session(request: Request, call_next):
        path = request.url.path
        public_deployment = path == "/api/deployment-status" and request.method == "GET"
        if path.startswith("/api/") and path not in OPEN_PATHS and not public_deployment:
            token = request.cookies.get(SESSION_COOKIE)
            try:
                if not token:
                    raise Unauthorized("no session")
                request.state.read_only = token.startswith("viewer.")
                if request.state.read_only:
                    if password is None:
                        raise Unauthorized("viewer login disabled")
                    claims = jwt.decode(
                        token.removeprefix("viewer."),
                        viewer_key,
                        algorithms=["HS256"],
                        audience=settings.console_url,
                        issuer="mel-viewer",
                        options={"require": ["exp", "sub", "role", "aud", "iss"]},
                    )
                    if claims["role"] != "viewer":
                        raise Unauthorized("invalid viewer role")
                    request.state.user = {"username": "Viewer"}
                else:
                    request.state.user = await asyncio.to_thread(verifier.claims, token)
            except (Unauthorized, jwt.PyJWTError):
                return JSONResponse({"detail": "Sign in required"}, status_code=401)
            # AgentCore option discovery invokes paid runtime compute, and /api/voice spends
            # ElevenLabs credits, even on GET.
            if request.state.read_only and (
                request.method not in {"GET", "HEAD", "OPTIONS"} or path in PAID_GETS
            ):
                return JSONResponse(
                    {"detail": "View-only access: changes and runs are disabled"}, status_code=403
                )
        return await call_next(request)

    @app.get("/api/auth/login")
    def login():
        state, code_verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": settings.auth_client_id,
                "redirect_uri": callback,
                "scope": "openid email",
                "state": state,
                "code_challenge_method": "S256",
                "code_challenge": _challenge(code_verifier),
            }
        )
        response = RedirectResponse(f"{settings.auth_domain}/oauth2/authorize?{query}", 302)
        # Lax, not Strict: the browser must send it on the redirect back from Cognito.
        response.set_cookie(
            SIGN_IN_COOKIE,
            f"{state}.{code_verifier}",
            max_age=SIGN_IN_SECONDS,
            path="/api/auth",
            httponly=True,
            secure=True,
            samesite="lax",
        )
        return response

    @app.get("/api/auth/callback")
    async def finish_sign_in(request: Request, code: str, state: str):
        pending = request.cookies.get(SIGN_IN_COOKIE, "")
        expected, _, code_verifier = pending.partition(".")
        if not code_verifier or not secrets.compare_digest(expected, state):
            raise HTTPException(400, "Sign-in expired or did not match; start again")
        tokens = await exchange(code, code_verifier, callback)
        try:
            await asyncio.to_thread(verifier.claims, tokens["access_token"])
        except (KeyError, Unauthorized):
            raise HTTPException(502, "Cognito returned an unusable token") from None
        response = RedirectResponse(f"{settings.console_url}/", 302)
        response.set_cookie(
            SESSION_COOKIE,
            tokens["access_token"],
            max_age=int(tokens["expires_in"]),
            path="/api",
            httponly=True,
            secure=True,
            samesite="strict",
        )
        response.delete_cookie(SIGN_IN_COOKIE, path="/api/auth", secure=True, httponly=True)
        return response

    @app.get("/api/auth/logout")
    def logout(request: Request):
        if request.cookies.get(SESSION_COOKIE, "").startswith("viewer."):
            response = RedirectResponse(f"{settings.console_url}/", 302)
            response.delete_cookie(SESSION_COOKIE, path="/api", secure=True, httponly=True)
            return response
        query = urlencode(
            {"client_id": settings.auth_client_id, "logout_uri": f"{settings.console_url}/"}
        )
        response = RedirectResponse(f"{settings.auth_domain}/logout?{query}", 302)
        response.delete_cookie(SESSION_COOKIE, path="/api", secure=True, httponly=True)
        return response

    @app.get("/api/auth/session")
    def session(request: Request):
        if request.state.read_only:
            return {"auth": True, "user": "Viewer", "read_only": True}
        return {"auth": True, "user": request.state.user.get("username")}


def _exchange(settings: Settings):
    async def exchange(code: str, code_verifier: str, redirect_uri: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{settings.auth_domain}/oauth2/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": settings.auth_client_id,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
            )
        if response.status_code != 200:
            raise HTTPException(502, f"Cognito token exchange failed ({response.status_code})")
        return response.json()

    return exchange
