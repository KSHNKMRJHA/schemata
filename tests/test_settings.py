"""Settings surface: .env writer, cache reload, and the /api/settings endpoints."""

import pytest

from app import config


@pytest.fixture(autouse=True)
def _reset_env(tmp_path, monkeypatch, request):
    """Isolate .env writes to a temp file and clear caches between tests."""
    from app.orchestrator import reload_orchestrator
    from app.sources.digikey import reset_token_cache

    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    for key in ("MOUSER_API_KEY", "DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    config.get_settings.cache_clear()
    config.config_section.cache_clear()
    reload_orchestrator()
    reset_token_cache()
    yield
    config.get_settings.cache_clear()
    config.config_section.cache_clear()
    reload_orchestrator()
    reset_token_cache()


def test_write_env_creates_and_updates_preserving_other_lines():
    config.ENV_PATH.write_text(
        "# demo note\nDEMO_DATA=true\nMOUSER_API_KEY=old\n",
        encoding="utf-8",
    )
    written = config.write_env(
        {"mouser_api_key": "new-key", "digikey_client_id": "cid", "digikey_client_secret": "csecret"}
    )
    assert written == ["digikey_client_id", "digikey_client_secret", "mouser_api_key"]
    text = config.ENV_PATH.read_text(encoding="utf-8")
    assert "# demo note" in text
    assert "DEMO_DATA=true" in text
    assert "MOUSER_API_KEY=new-key" in text
    assert "DIGIKEY_CLIENT_ID=cid" in text
    assert "DIGIKEY_CLIENT_SECRET=csecret" in text


def test_write_env_skips_empty_values():
    config.ENV_PATH.write_text("MOUSER_API_KEY=old\n", encoding="utf-8")
    written = config.write_env({"mouser_api_key": "", "digikey_client_id": "cid"})
    assert written == ["digikey_client_id"]
    text = config.ENV_PATH.read_text(encoding="utf-8")
    assert "MOUSER_API_KEY=old" in text
    assert "DIGIKEY_CLIENT_ID=cid" in text


def test_write_env_rejects_masked_placeholder():
    with pytest.raises(ValueError, match="masked placeholder"):
        config.write_env({"mouser_api_key": "••••••••abcd"})


def test_settings_page_does_not_leak_mask_into_input_value():
    from fastapi.testclient import TestClient

    from app import main

    with TestClient(main.app) as client:
        resp = client.get("/settings")
    assert resp.status_code == 200
    assert "value=\"" in resp.text
    assert "••" not in resp.text


def test_reload_settings_picks_up_written_keys():
    config.write_env({"mouser_api_key": "live-key"})
    config.reload_settings()
    from app.config import get_settings

    assert get_settings().mouser_api_key == "live-key"


def test_settings_api_save_updates_configured_state():
    from fastapi.testclient import TestClient

    from app import main

    with TestClient(main.app) as client:
        resp = client.post(
            "/api/settings",
            json={"mouser_api_key": "m-123", "digikey_client_id": "", "digikey_client_secret": ""},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["configured"]["mouser"] is True
    assert data["configured"]["digikey"] is False
    assert "Mouser" in data["live"]
    assert config.ENV_PATH.exists()
    assert "MOUSER_API_KEY=m-123" in config.ENV_PATH.read_text(encoding="utf-8")


def test_settings_api_test_marks_demo_sources_without_network():
    from fastapi.testclient import TestClient

    from app import main

    with TestClient(main.app) as client:
        resp = client.post("/api/settings/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["probe"]
    assert data["sources"]
    # no keys configured -> every source is demo, nothing hits the network
    for entry in data["sources"].values():
        assert entry["configured"] is False
        assert "demo" in entry["note"]
