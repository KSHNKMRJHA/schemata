"""Bring-your-own-key (BYOK): per-request ephemeral credentials.

Tests the three layers:
  1. Credentials.with_overrides — shared-backend copy with per-request keys.
  2. Config.with_credential_overrides — full config clone swapping credentials.
  3. Routes — X-Schemata-Keys header parsing + body credentials merging.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.engine.bomiq.config import Config, Credentials
from app.main import app

# ---------------------------------------------------------------------------
# 1. Credentials.with_overrides
# ---------------------------------------------------------------------------

class TestCredentialsOverrides:
    """Unit tests for the ephemeral-override layer on Credentials."""

    def test_overrides_checked_first(self, tmp_path: Path) -> None:
        cred = Credentials(config_dir=tmp_path, use_keyring=False,
                           overrides={"digikey": {"client_id": "oid-123"}})
        assert cred.get("digikey", "client_id") == "oid-123"

    def test_fallback_to_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("MOUSER_API_KEY", "env-key")
        cred = Credentials(config_dir=tmp_path, use_keyring=False)
        assert cred.get("mouser", "api_key") == "env-key"

    def test_override_wins_over_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("MOUSER_API_KEY", "env-key")
        cred = Credentials(config_dir=tmp_path, use_keyring=False,
                           overrides={"mouser": {"api_key": "browser-key"}})
        assert cred.get("mouser", "api_key") == "browser-key"

    def test_original_not_mutated(self, tmp_path: Path) -> None:
        original = Credentials(config_dir=tmp_path, use_keyring=False)
        clone = original.with_overrides({"digikey": {"client_id": "x"}})
        assert original.get("digikey", "client_id") == ""
        assert clone.get("digikey", "client_id") == "x"

    def test_get_all_includes_overrides(self, tmp_path: Path) -> None:
        cred = Credentials(config_dir=tmp_path, use_keyring=False,
                           overrides={"mouser": {"api_key": "browser"}})
        all_keys = cred.get_all("mouser")
        assert all_keys["api_key"] == "browser"

    def test_override_value_must_be_string(self, tmp_path: Path) -> None:
        """Non-string values in overrides are coerced to str."""
        cred = Credentials(config_dir=tmp_path, use_keyring=False,
                           overrides={"mouser": {"api_key": 12345}})
        assert cred.get("mouser", "api_key") == "12345"


# ---------------------------------------------------------------------------
# 2. Config.with_credential_overrides
# ---------------------------------------------------------------------------

class TestConfigCredentialOverrides:
    """Config clone swaps .credentials while sharing everything else."""

    def test_creates_independent_credentials(self) -> None:
        config = Config()
        clone = config.with_credential_overrides(
            {"digikey": {"client_id": "test-id"}})
        assert clone.credentials is not config.credentials
        assert clone.credentials.get("digikey", "client_id") == "test-id"

    def test_original_config_unmodified(self) -> None:
        config = Config()
        original_id = config.credentials.get("digikey", "client_id")
        config.with_credential_overrides(
            {"digikey": {"client_id": "test-id"}})
        assert config.credentials.get("digikey", "client_id") == original_id

    def test_shares_config_dir(self) -> None:
        config = Config()
        clone = config.with_credential_overrides(
            {"mouser": {"api_key": "x"}})
        assert clone.config_dir == config.config_dir

    def test_empty_overrides_still_clone(self) -> None:
        config = Config()
        clone = config.with_credential_overrides(None)
        assert clone.credentials is not config.credentials


# ---------------------------------------------------------------------------
# 3. Routes — header parsing + body credentials
# ---------------------------------------------------------------------------

def _encode_header(obj: dict) -> str:
    """Base64url-encode a dict for X-Schemata-Keys."""
    raw = json.dumps(obj).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class TestByokRouteHelpers:
    """Unit-test the internal route helpers via the TestClient."""

    def test_valid_header_reaches_engine(self) -> None:
        """X-Schemata-Keys header is parsed and forwarded to the engine."""
        header = _encode_header({"mock": {"api_key": "test"}})
        with TestClient(app) as c:
            resp = c.post("/api/providers/test",
                          json={"providers": ["mock"]},
                          headers={"X-Schemata-Keys": header})
        assert resp.status_code == 200
        results = resp.json().get("results", [])
        assert len(results) >= 1

    def test_body_credentials_merge_with_header(self) -> None:
        """Body credentials and header credentials are merged (header wins)."""
        header = _encode_header({"mock": {"api_key": "from-header"}})
        with TestClient(app) as c:
            resp = c.post("/api/providers/test",
                          json={"providers": ["mock"],
                                "credentials": {"mock": {"api_key": "from-body"}}},
                          headers={"X-Schemata-Keys": header})
        assert resp.status_code == 200

    def test_invalid_header_returns_400(self) -> None:
        with TestClient(app) as c:
            resp = c.post("/api/providers/test",
                          json={"providers": ["mock"]},
                          headers={"X-Schemata-Keys": "not-valid-base64!!"})
        assert resp.status_code == 400
        assert "X-Schemata-Keys" in resp.json()["detail"]

    def test_unknown_provider_in_header_is_ignored(self) -> None:
        header = _encode_header({"nonexistent_provider_xyz": {"api_key": "x"}})
        with TestClient(app) as c:
            resp = c.post("/api/providers/test",
                          json={"providers": ["mock"]},
                          headers={"X-Schemata-Keys": header})
        assert resp.status_code == 200

    def test_empty_header_is_ignored(self) -> None:
        with TestClient(app) as c:
            resp = c.post("/api/providers/test",
                          json={"providers": ["mock"]},
                          headers={"X-Schemata-Keys": ""})
        assert resp.status_code == 200

    def test_byok_header_on_get_part(self) -> None:
        """GET /api/part accepts X-Schemata-Keys (no body)."""
        header = _encode_header({"mock": {"api_key": "test"}})
        with TestClient(app) as c:
            resp = c.get("/api/part",
                         params={"mpn": "MCU-ATMEGA328P"},
                         headers={"X-Schemata-Keys": header})
        assert resp.status_code == 200

    def test_body_credentials_object_on_search(self) -> None:
        """POST /api/search accepts credentials in the JSON body."""
        with TestClient(app) as c:
            resp = c.post("/api/search",
                          json={"query": "ATMEGA328P",
                                "credentials": {"mock": {"api_key": "k"}}})
        assert resp.status_code == 200
        assert resp.json()["count"] >= 1  # offline catalogue match


# ---------------------------------------------------------------------------
# 4. Bootstrap signals open mode
# ---------------------------------------------------------------------------

class TestBootstrapPublicSignal:
    """The bootstrap endpoint reports whether the instance is public."""

    def test_public_true_when_no_token(self, monkeypatch) -> None:
        monkeypatch.delenv("SCHEMATA_ACCESS_TOKEN", raising=False)
        with TestClient(app) as c:
            resp = c.get("/api/bootstrap",
                         headers={"X-BOMIQ-Token": ""})
        assert resp.status_code == 200
        app_info = resp.json().get("app", {})
        assert "public" in app_info
        assert app_info["public"] is True
