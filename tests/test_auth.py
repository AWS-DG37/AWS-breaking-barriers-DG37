"""Hosted console sign-in: cookies only, checked against the issuer's signing keys."""

import hashlib
import re
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from test_explorer_api import Store

from mel.config import Settings
from mel.explorer.api import create_app
from mel.explorer.auth import SESSION_COOKIE, SIGN_IN_COOKIE, TokenVerifier, _challenge

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_test"
CONSOLE = "https://console.example"
SETTINGS = Settings(
    bedrock_model_id="test",
    auth_domain="https://signin.example",
    auth_client_id="console-client",
    auth_issuer=ISSUER,
    console_url=CONSOLE,
)
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Keys:
    def get_signing_key_from_jwt(self, token):
        return SimpleNamespace(key=KEY.public_key())


def token(key=KEY, **changes):
    claims = {
        "iss": ISSUER,
        "client_id": "console-client",
        "token_use": "access",
        "username": "reviewer",
        "exp": int(time.time()) + 3600,
        **changes,
    }
    return jwt.encode(claims, key, algorithm="RS256")


def client(exchange=None, *, settings=SETTINGS, store=None, agent_invoker=None):
    app = create_app(
        settings=settings,
        store_factory=lambda _: store if store is not None else Store(),
        auth_verifier=TokenVerifier(ISSUER, "console-client", keys=Keys()),
        auth_exchange=exchange,
        agent_invoker=agent_invoker,
    )
    # The cookies are Secure, so the test client must speak HTTPS to send them back.
    return TestClient(app, base_url="https://console.example", follow_redirects=False)


def test_talk_is_public_while_sign_in_guards_the_api():
    with client() as http:
        bare = http.get("/talk")
        assert bare.status_code == 307
        # Relative, so the redirect never names the host behind CloudFront.
        assert bare.headers["location"] == "/talk/"
        page = http.get("/talk/")
        assert page.status_code == 200
        assert "Field Echo" in page.text
        for path in re.findall(r'(?:src|href)="(?!https:)([^"#?]+)', page.text):
            resource = http.get(f"/talk/{path}")
            assert resource.status_code == 200, path
            assert not resource.headers["content-type"].startswith("text/html"), path
        assert http.get("/api/experiments").status_code == 401


@pytest.mark.parametrize(
    "value",
    [
        None,
        token(key=OTHER_KEY),
        token(exp=int(time.time()) - 3600),
        token(client_id="another-client"),
        token(token_use="id"),
        token(iss="https://cognito-idp.us-east-1.amazonaws.com/other"),
    ],
    ids=["missing", "wrong-signature", "expired", "other-client", "id-token", "other-issuer"],
)
def test_api_requires_a_valid_access_token_cookie(value):
    with client() as http:
        if value:
            http.cookies.set(SESSION_COOKIE, value)
        assert http.get("/api/experiments").status_code == 401


def test_a_valid_session_reaches_the_api_and_reports_the_user():
    with client() as http:
        http.cookies.set(SESSION_COOKIE, token())
        assert http.get("/api/experiments").status_code == 200
        assert http.get("/api/auth/session").json() == {"auth": True, "user": "reviewer"}


def test_a_bearer_header_is_not_a_session():
    with client() as http:
        response = http.get("/api/experiments", headers={"Authorization": f"Bearer {token()}"})
    assert response.status_code == 401


def test_login_redirects_to_cognito_with_pkce_and_a_lax_sign_in_cookie():
    with client() as http:
        response = http.get("/api/auth/login")
    assert response.status_code == 302
    target = urlparse(response.headers["location"])
    query = {k: v[0] for k, v in parse_qs(target.query).items()}
    assert (
        f"{target.scheme}://{target.netloc}{target.path}"
        == "https://signin.example/oauth2/authorize"
    )
    assert query["redirect_uri"] == f"{CONSOLE}/api/auth/callback"
    assert query["code_challenge_method"] == "S256"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SIGN_IN_COOKIE}=")
    assert all(part in cookie for part in ("HttpOnly", "Secure", "SameSite=lax", "Path=/api/auth"))
    state, _, verifier = cookie.split(";")[0].split("=", 1)[1].strip('"').partition(".")
    assert query["state"] == state
    assert query["code_challenge"] == _challenge(verifier)


def test_callback_sets_a_strict_session_cookie_after_verifying_the_token():
    seen = {}

    async def exchange(code, verifier, redirect_uri):
        seen.update(code=code, verifier=verifier, redirect_uri=redirect_uri)
        return {"access_token": token(), "expires_in": 3600}

    with client(exchange) as http:
        http.cookies.set(SIGN_IN_COOKIE, "state-1.verifier-1", path="/api/auth")
        response = http.get("/api/auth/callback", params={"code": "c-1", "state": "state-1"})
    assert response.status_code == 302
    assert response.headers["location"] == f"{CONSOLE}/"
    assert seen == {
        "code": "c-1",
        "verifier": "verifier-1",
        "redirect_uri": f"{CONSOLE}/api/auth/callback",
    }
    cookies = response.headers.get_list("set-cookie")
    session = next(c for c in cookies if c.startswith(f"{SESSION_COOKIE}="))
    assert all(part in session for part in ("HttpOnly", "Secure", "SameSite=strict", "Path=/api"))
    assert any(c.startswith(f"{SIGN_IN_COOKIE}=") and "Max-Age=0" in c for c in cookies)


@pytest.mark.parametrize("cookie", [None, "other-state.verifier"])
def test_callback_refuses_a_missing_or_mismatched_state(cookie):
    async def exchange(*args):
        pytest.fail("no token exchange expected")

    with client(exchange) as http:
        if cookie:
            http.cookies.set(SIGN_IN_COOKIE, cookie, path="/api/auth")
        response = http.get("/api/auth/callback", params={"code": "c", "state": "state-1"})
    assert response.status_code == 400


def test_callback_refuses_a_token_for_another_client():
    async def exchange(*args):
        return {"access_token": token(client_id="another-client"), "expires_in": 3600}

    with client(exchange) as http:
        http.cookies.set(SIGN_IN_COOKIE, "state-1.verifier-1", path="/api/auth")
        response = http.get("/api/auth/callback", params={"code": "c", "state": "state-1"})
    assert response.status_code == 502
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")


def test_logout_clears_the_session_and_signs_out_of_cognito():
    with client() as http:
        response = http.get("/api/auth/logout")
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://signin.example/logout?")
    assert "logout_uri=https%3A%2F%2Fconsole.example%2F" in response.headers["location"]
    cleared = response.headers["set-cookie"]
    assert cleared.startswith(f"{SESSION_COOKIE}=") and "Max-Age=0" in cleared


def test_without_sign_in_settings_the_local_console_is_open():
    app = create_app(settings=Settings(bedrock_model_id="test"), store_factory=lambda _: Store())
    with TestClient(app) as http:
        assert http.get("/api/auth/session").json() == {"auth": False}
        assert http.get("/api/experiments").status_code == 200


def test_partial_sign_in_settings_are_rejected():
    with pytest.raises(ValueError, match="auth_domain"):
        Settings(auth_domain="https://signin.example")


VIEWER_PASSWORD = "shared-viewer-test-password"


def viewer_settings(**changes):
    return Settings(**{**SETTINGS.model_dump(), "viewer_password": VIEWER_PASSWORD, **changes})


def test_viewer_password_creates_a_secure_read_only_session():
    with client(settings=viewer_settings()) as http:
        response = http.post("/api/auth/viewer", json={"password": VIEWER_PASSWORD})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert all(p in cookie for p in ("HttpOnly", "Secure", "SameSite=strict", "Path=/api"))
        assert VIEWER_PASSWORD not in cookie
        assert http.get("/api/auth/session").json() == {
            "auth": True,
            "user": "Viewer",
            "read_only": True,
        }
        assert http.get("/api/experiments").status_code == 200


@pytest.mark.parametrize("password", ["wrong", "", "é" * 20])
def test_incorrect_viewer_password_never_creates_a_session(password):
    with client(settings=viewer_settings()) as http:
        response = http.post("/api/auth/viewer", json={"password": password})
        assert response.status_code == 401
        assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
        assert http.get("/api/experiments").status_code == 401


def test_viewer_login_is_disabled_until_a_password_is_configured():
    with client() as http:
        assert http.post("/api/auth/viewer", json={"password": VIEWER_PASSWORD}).status_code == 404
        assert http.get("/api/auth/options").json() == {"auth": True, "viewer": False}
    with client(settings=viewer_settings()) as http:
        assert http.get("/api/auth/options").json() == {"auth": True, "viewer": True}


def test_viewer_login_is_throttled_after_repeated_wrong_passwords():
    with client(settings=viewer_settings()) as http:
        for _ in range(5):
            assert http.post("/api/auth/viewer", json={"password": "wrong"}).status_code == 401
        response = http.post("/api/auth/viewer", json={"password": VIEWER_PASSWORD})
        assert response.status_code == 429
        assert int(response.headers["retry-after"]) > 0


def test_tampered_viewer_cookie_and_rotated_password_are_rejected():
    with client(settings=viewer_settings()) as http:
        assert http.post("/api/auth/viewer", json={"password": VIEWER_PASSWORD}).status_code == 200
        original = http.cookies.get(SESSION_COOKIE)
        http.cookies.clear()
        http.cookies.set(SESSION_COOKIE, original[:-10] + "0123456789")
        assert http.get("/api/experiments").status_code == 401
    with client(settings=viewer_settings(viewer_password="a-different-shared-password")) as http:
        http.cookies.set(SESSION_COOKIE, original)
        assert http.get("/api/experiments").status_code == 401
    with client() as http:
        http.cookies.set(SESSION_COOKIE, original)
        assert http.get("/api/experiments").status_code == 401


def test_viewer_session_expires(monkeypatch):
    from datetime import UTC, datetime, timedelta

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(UTC) + timedelta(hours=9)

    with client(settings=viewer_settings()) as http:
        assert http.post("/api/auth/viewer", json={"password": VIEWER_PASSWORD}).status_code == 200
        monkeypatch.setattr(jwt.api_jwt, "datetime", Later)
        assert http.get("/api/experiments").status_code == 401


WRITE_ROUTES = [
    (method, route.path)
    for route in create_app(settings=SETTINGS).routes
    for method in getattr(route, "methods", set()) - {"GET", "HEAD", "OPTIONS"}
    if route.path.startswith("/api/") and not route.path.startswith("/api/auth/")
]


@pytest.mark.parametrize("method,path", WRITE_ROUTES)
def test_viewers_cannot_reach_any_write_handler(method, path):
    with client(settings=viewer_settings()) as http:
        assert http.post("/api/auth/viewer", json={"password": VIEWER_PASSWORD}).status_code == 200
        response = http.request(method, path, json={})
        assert response.status_code == 403
        assert "view-only" in response.json()["detail"].lower()


def test_viewer_config_read_does_not_seed_or_activate_a_version():
    store = Store()
    with client(settings=viewer_settings(), store=store) as http:
        assert http.post("/api/auth/viewer", json={"password": VIEWER_PASSWORD}).status_code == 200
        response = http.get("/api/config")
        assert response.status_code == 200
        assert response.json()["active_config_id"] is None
        assert response.json()["live_enabled"] is False
        assert store.active_config_id() is None
        assert store.configs == {}


@pytest.mark.parametrize(
    "role,read_status,write_status", [("viewer", 200, 403), ("admin", 401, 401)]
)
def test_password_holder_cannot_escalate_or_spend_with_a_long_lived_cookie(
    role, read_status, write_status
):
    key = hashlib.sha256(f"mel-viewer-session:{VIEWER_PASSWORD}".encode()).digest()
    cookie = jwt.encode(
        {
            "sub": "Viewer",
            "role": role,
            "aud": CONSOLE,
            "iss": "mel-viewer",
            "exp": int(time.time()) + 365 * 24 * 3600,
        },
        key,
        algorithm="HS256",
    )
    with client(settings=viewer_settings()) as http:
        http.cookies.set(SESSION_COOKIE, f"viewer.{cookie}")
        assert http.get("/api/experiments").status_code == read_status
        for method, path in WRITE_ROUTES:
            assert http.request(method, path, json={}).status_code == write_status


def test_cognito_operators_keep_write_access_with_viewer_login_enabled():
    with client(settings=viewer_settings()) as http:
        http.cookies.set(SESSION_COOKIE, token())
        assert http.post("/api/configs", json={}).status_code == 422


def test_viewer_password_requires_hosted_authentication_and_a_nonempty_secret():
    with pytest.raises(ValueError):
        Settings(viewer_password=VIEWER_PASSWORD)
    with pytest.raises(ValueError):
        viewer_settings(viewer_password="")


def test_viewers_cannot_invoke_agentcore_even_for_get_options():
    calls = []

    def invoke(payload):
        calls.append(payload)
        return {"ok": True, "result": {"models": []}}

    with client(settings=viewer_settings(), agent_invoker=invoke) as http:
        assert http.post("/api/auth/viewer", json={"password": VIEWER_PASSWORD}).status_code == 200
        assert http.get("/api/agents/options").status_code == 403
        assert http.post("/api/agents/runs", json={"insight_id": "I-004512"}).status_code == 403
        assert http.post("/api/agents/decide", json={"insight_id": "I-004512"}).status_code == 403
        assert calls == []


def test_operators_keep_agentcore_option_access():
    calls = []

    def invoke(payload):
        calls.append(payload)
        return {"ok": True, "result": {"models": []}}

    with client(settings=viewer_settings(), agent_invoker=invoke) as http:
        http.cookies.set(SESSION_COOKIE, token())
        response = http.get("/api/agents/options")
        assert response.status_code == 200
        assert response.json() == {"configured": True, "models": []}
        assert calls == [{"action": "options"}]
