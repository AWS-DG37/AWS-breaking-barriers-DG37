"""Hosted console sign-in: cookies only, checked against the issuer's signing keys."""

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


def client(exchange=None):
    app = create_app(
        settings=SETTINGS,
        store_factory=lambda _: Store(),
        auth_verifier=TokenVerifier(ISSUER, "console-client", keys=Keys()),
        auth_exchange=exchange,
    )
    # The cookies are Secure, so the test client must speak HTTPS to send them back.
    return TestClient(app, base_url="https://console.example", follow_redirects=False)


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
