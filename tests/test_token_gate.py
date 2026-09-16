"""Access-token gate for online deployments (SCHEMATA_ACCESS_TOKEN).

The gate applies only when the env var is set, so local runs stay open.
"""

import os

from fastapi.testclient import TestClient

from app.main import app

_TOKEN = "test-sekrit-token"


def _client() -> TestClient:
    return TestClient(app)


def teardown_function() -> None:
    os.environ.pop("SCHEMATA_ACCESS_TOKEN", None)


def test_open_when_no_token_configured() -> None:
    os.environ.pop("SCHEMATA_ACCESS_TOKEN", None)
    with _client() as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/parts").status_code == 200


def test_healthz_is_exempt(monkeypatch) -> None:
    monkeypatch.setenv("SCHEMATA_ACCESS_TOKEN", _TOKEN)
    with _client() as c:
        assert c.get("/healthz").status_code == 200


def test_requires_token(monkeypatch) -> None:
    monkeypatch.setenv("SCHEMATA_ACCESS_TOKEN", _TOKEN)
    with _client() as c:
        assert c.get("/parts").status_code == 401
        assert c.get("/parts", params={"token": "wrong"}).status_code == 401


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
