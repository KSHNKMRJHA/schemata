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
    arrow_api_key: str = ""
    arrow_login: str = ""
    farnell_api_key: str = ""
    farnell_store: str = ""
    lcsc_api_key: str = ""
    lcsc_api_secret: str = ""
    trustedparts_api_key: str = ""
    database_url: str = f"sqlite:///{DATA_DIR / 'part_intel.db'}"
    demo_data: bool = True


def _load_toml() -> dict:
    if not TOML_PATH.exists():
        return {}
    with TOML_PATH.open("rb") as fh:
        return tomllib.load(fh)


def _defaults() -> dict:
    return _load_toml().get("defaults", {})


def _cache_cfg() -> dict:
    return _load_toml().get("cache", {})


def _rate_cfg() -> dict:
    return _load_toml().get("rate_limits", {})


def _risk_cfg() -> dict:
    return _load_toml().get("risk", {})


@lru_cache(maxsize=1)
def get_settings() -> Secrets:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Secrets(_env_file=str(ENV_PATH) if ENV_PATH.exists() else None)


@lru_cache(maxsize=1)
def config_section(section: str) -> dict:
    if section == "defaults":
        return _defaults()
    if section == "cache":
        return _cache_cfg()
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
