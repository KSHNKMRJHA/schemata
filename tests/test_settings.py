"""Settings surface: the SPA-facing /api/settings and /api/providers endpoints."""

import pytest


@pytest.fixture(autouse=True)
def _reset_engine(tmp_path, monkeypatch):
    """Isolate engine config/credentials into a temp dir and reset singletons."""
    from app.engine import bridge
    from app.engine.bomiq import config as bomiq_config

    monkeypatch.setenv("SCHEMATA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCHEMATA_CONFIG_DIR", str(tmp_path / "config"))
    for key in (
        "MOUSER_API_KEY", "DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET",
        "NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET",
        "BOMIQ_MOUSER_API_KEY", "BOMIQ_DIGIKEY_CLIENT_ID",
        "BOMIQ_DIGIKEY_CLIENT_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)
    bridge.reset_engine()
    bomiq_config.reset_config()
    yield
    bridge.reset_engine()
    bomiq_config.reset_config()


def _client():
    from fastapi.testclient import TestClient

    from app import main

    return TestClient(main.app)


def test_get_settings_returns_engine_dict():
    with _client() as client:
        resp = client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert "build_quantity" in data
    assert "currency" in data
    assert data["build_quantity"] == 100


def test_post_settings_updates_engine_settings():
    with _client() as client:
        resp = client.post("/api/settings", json={"build_quantity": 250})
    assert resp.status_code == 200
    data = resp.json()
    assert "build_quantity" in data["changed"]
    assert data["settings"]["build_quantity"] == 250
    assert isinstance(data["settings"]["currency"], str)
    # no keys configured -> only the offline catalogue is usable
    assert data["enabled_providers"] == ["mock"]


def test_post_settings_ignores_unknown_keys():
    with _client() as client:
        resp = client.post("/api/settings", json={"not_a_setting": 42})
    assert resp.status_code == 200
    data = resp.json()
    assert data["changed"] == []
    assert "not_a_setting" not in data["settings"]


def test_credentials_set_then_clear():
    with _client() as client:
        resp = client.post(
            "/api/providers/mouser/credentials",
            json={"credentials": {"api_key": "k-123"}},
        )
    assert resp.status_code == 200
    assert resp.json()["configured"] is True

    with _client() as client:
        resp = client.get("/api/settings")
    assert resp.status_code == 200

    with _client() as client:
        resp = client.delete("/api/providers/mouser/credentials")
    assert resp.status_code == 200
    assert resp.json()["configured"] is False


def test_credentials_reject_unknown_provider():
    with _client() as client:
        resp = client.post(
            "/api/providers/nope/credentials",
            json={"credentials": {"api_key": "x"}},
        )
    assert resp.status_code == 404


def test_credentials_reject_empty_fieldset():
    with _client() as client:
        resp = client.post(
            "/api/providers/mouser/credentials",
            json={"credentials": {"not_a_key": "x"}},
        )
    assert resp.status_code == 400
    assert "api_key" in str(resp.json()["detail"])


def test_test_providers_runs_offline_without_network():
    with _client() as client:
        resp = client.post("/api/providers/test", json={"providers": []})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert isinstance(results, list)
    assert results, "the offline catalogue must always produce a self-test"
    for entry in results:
        assert "provider" in entry
