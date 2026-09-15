"""Configuration: .env secrets + config.toml defaults."""

from __future__ import annotations

import os
import sys
import tomllib
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _runtime_base() -> Path:
    """Where the app's read-only files (ui/, config.toml) live.

    Source tree -> repo root; frozen binary -> the dist dir next to the exe.
    """
    if FROZEN:
        base = getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent
        return Path(base)
    return Path(__file__).resolve().parent.parent


def _user_data_dir() -> Path:
    """Writable location for the DB, exports and .env on installed builds."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    root = Path(base) / "Schemata"
    root.mkdir(parents=True, exist_ok=True)
    return root


FROZEN: bool = bool(globals().get("__compiled__")) or bool(getattr(sys, "frozen", False))
BASE_DIR = _runtime_base()
DATA_DIR = _user_data_dir() / "data" if FROZEN else BASE_DIR / "data"
UI_DIR = BASE_DIR / "ui"
FIXTURES_DIR = BASE_DIR / "tests" / "fixtures"
TOML_PATH = BASE_DIR / "config.toml"
ENV_PATH = _user_data_dir() / ".env" if FROZEN else BASE_DIR / ".env"


class Secrets(BaseSettings):
    """Loaded from .env (or process environment)."""

    model_config = SettingsConfigDict(env_file_encoding="utf-8", extra="ignore")

    mouser_api_key: str = ""
    digikey_client_id: str = ""
    digikey_client_secret: str = ""
    digikey_sandbox: bool = False
    nexar_client_id: str = ""
    nexar_client_secret: str = ""
    database_url: str = f"sqlite:///{DATA_DIR / 'part_intel.db'}"
    demo_data: bool = True


_ENV_KEYS = (
    "mouser_api_key",
    "digikey_client_id",
    "digikey_client_secret",
    "digikey_sandbox",
    "nexar_client_id",
    "nexar_client_secret",
)


def _load_toml() -> dict:
    if not TOML_PATH.exists():
        return {}
    with TOML_PATH.open("rb") as fh:
        return tomllib.load(fh)


def _defaults() -> dict:
    return _load_toml().get("defaults", {})


def _cache_cfg() -> dict:
    return _load_toml().get("cache", {})


def _orchestrator_cfg() -> dict:
    return _load_toml().get("orchestrator", {})


def _rate_cfg() -> dict:
    return _load_toml().get("rate_limits", {})


def _risk_cfg() -> dict:
    return _load_toml().get("risk", {})


@lru_cache(maxsize=1)
def get_settings() -> Secrets:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Secrets(_env_file=str(ENV_PATH) if ENV_PATH.exists() else None)


def reload_settings() -> None:
    """Forget cached config after the .env is rewritten so the next read sees new values."""
    get_settings.cache_clear()
    config_section.cache_clear()


_FIELD_TO_ENV = {
    "mouser_api_key": "MOUSER_API_KEY",
    "digikey_client_id": "DIGIKEY_CLIENT_ID",
    "digikey_client_secret": "DIGIKEY_CLIENT_SECRET",
    "digikey_sandbox": "DIGIKEY_SANDBOX",
    "nexar_client_id": "NEXAR_CLIENT_ID",
    "nexar_client_secret": "NEXAR_CLIENT_SECRET",
}


def write_env(overrides: dict[str, str]) -> list[str]:
    """Write credential overrides to .env, preserving other keys/comments.

    Returns the list of field names that were actually persisted (non-empty).
    """
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if ENV_PATH.exists():
        existing = ENV_PATH.read_text(encoding="utf-8")
    lines = existing.splitlines()
    changed: set[str] = set()
    for field, value in overrides.items():
        env_key = _FIELD_TO_ENV.get(field, field)
        if isinstance(value, bool):
            value = "true" if value else "false"
        else:
            value = (value or "").strip()
            if not value:
                continue
        if "•" in value:
            raise ValueError(f"Refusing to store a masked placeholder as a real credential: {field}")
        pattern = f"{env_key}="
        replaced = False
        for i, line in enumerate(lines):
            if line.strip().startswith(pattern):
                lines[i] = f"{pattern}{value}"
                replaced = True
                changed.add(field)
                break
        if not replaced:
            lines.append(f"{pattern}{value}")
            changed.add(field)
    merged = "\n".join(lines).rstrip() + ("\n" if lines else "")
    ENV_PATH.write_text(merged, encoding="utf-8")
    return sorted(changed)


@lru_cache(maxsize=1)
def config_section(section: str) -> dict:
    if section == "defaults":
        return _defaults()
    if section == "cache":
        return _cache_cfg()
    if section == "orchestrator":
        return _orchestrator_cfg()
    if section == "risk":
        return _risk_cfg()
    if section == "rate_limits":
        return _rate_cfg()
    return _load_toml().get(section, {})


def currency() -> str:
    return _defaults().get("currency", "USD")


def region() -> str:
    return _defaults().get("region", "US")


def site() -> str:
    return _defaults().get("site", "US")


def language() -> str:
    return _defaults().get("language", "en")
