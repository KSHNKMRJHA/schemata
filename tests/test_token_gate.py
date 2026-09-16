"""Access-token gate + login flow for online deployments (SCHEMATA_ACCESS_TOKEN).

The gate applies only when the env var is set, so local runs stay open.
Unauthenticated page requests are redirected to /login; /api/* returns 401 JSON.
"""

import os

from fastapi.testclient import TestClient

from app.main import app

_TOKEN = "test-sekrit-token"


def _client() -> TestClient:
    return TestClient(app)


def teardown_function() -> None:
    os.environ.pop("SCHEMATA_ACCESS_TOKEN", None)


# --- open when no token configured ---

def test_open_when_no_token_configured() -> None:
    os.environ.pop("SCHEMATA_ACCESS_TOKEN", None)
    with _client() as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/parts").status_code == 200
        assert c.get("/login").status_code == 200


# --- exempt routes ---

def test_healthz_is_exempt(monkeypatch) -> None:
    monkeypatch.setenv("SCHEMATA_ACCESS_TOKEN", _TOKEN)
    with _client() as c:
        assert c.get("/healthz").status_code == 200


def test_login_page_is_exempt(monkeypatch) -> None:
    monkeypatch.setenv("SCHEMATA_ACCESS_TOKEN", _TOKEN)
    with _client() as c:
        resp = c.get("/login")
        assert resp.status_code == 200
        assert "Access token" in resp.text


# --- unauthenticated page requests redirect to /login ---

def test_page_redirects_to_login(monkeypatch) -> None:
    monkeypatch.setenv("SCHEMATA_ACCESS_TOKEN", _TOKEN)
    with _client() as c:
        resp = c.get("/parts", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login?next=%2Fparts" in resp.headers["location"]


def test_wrong_token_redirects_to_login(monkeypatch) -> None:
    monkeypatch.setenv("SCHEMATA_ACCESS_TOKEN", _TOKEN)
    with _client() as c:
        resp = c.get("/parts?token=wrong", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["location"]


# --- /api/* returns 401 JSON ---

def test_api_returns_401_json(monkeypatch) -> None:
    monkeypatch.setenv("SCHEMATA_ACCESS_TOKEN", _TOKEN)
    with _client() as c:
        resp = c.get("/api/parts")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "unauthorized"


# --- valid token grants access ---

def test_query_token_grants_access_and_sets_cookie(monkeypatch) -> None:
    monkeypatch.setenv("SCHEMATA_ACCESS_TOKEN", _TOKEN)
    with _client() as c:
        response = c.get("/parts", params={"token": _TOKEN})
        assert response.status_code == 200
        assert "schemata_access" in c.cookies
        assert c.get("/parts").status_code == 200


def test_bearer_token_grants_access(monkeypatch) -> None:
    monkeypatch.setenv("SCHEMATA_ACCESS_TOKEN", _TOKEN)
    with _client() as c:
        response = c.get("/parts", headers={"Authorization": f"Bearer {_TOKEN}"})
        assert response.status_code == 200


def test_raw_token_with_plus_pasted_verbatim(monkeypatch) -> None:
    token = "abc+def=ghi"
    monkeypatch.setenv("SCHEMATA_ACCESS_TOKEN", token)
    with _client() as c:
        assert c.get("/parts?token=" + token).status_code == 200
    with _client() as c:
        assert c.get("/parts?token=wrong", follow_redirects=False).status_code == 302


# --- /login POST flow ---

def test_login_post_valid_token(monkeypatch) -> None:
    monkeypatch.setenv("SCHEMATA_ACCESS_TOKEN", _TOKEN)
    with _client() as c:
        resp = c.post("/login", data={"token": _TOKEN}, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/parts"
        assert "schemata_access" in c.cookies
        assert c.get("/parts").status_code == 200


def test_login_post_invalid_token(monkeypatch) -> None:
    monkeypatch.setenv("SCHEMATA_ACCESS_TOKEN", _TOKEN)
    with _client() as c:
        resp = c.post("/login", data={"token": "wrong"})
        assert resp.status_code == 200
        assert "Invalid token" in resp.text


def test_login_post_token_with_plus(monkeypatch) -> None:
    token = "abc+def=ghi"
    monkeypatch.setenv("SCHEMATA_ACCESS_TOKEN", token)
    with _client() as c:
        resp = c.post("/login", data={"token": token}, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/parts"


def test_login_redirects_home_when_no_token_env(monkeypatch) -> None:
    monkeypatch.delenv("SCHEMATA_ACCESS_TOKEN", raising=False)
    with _client() as c:
        resp = c.get("/login", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/parts"
