"""
Configuration, credentials and per-platform paths.

Precedence, highest first:

1. explicit keyword arguments / ``--set`` CLI overrides
2. environment variables (``BOMIQ_*``, plus the conventional provider names)
3. ``.env`` in the working directory or next to the executable
4. ``settings.json`` in the user config directory
5. the OS keyring (credentials only, when ``keyring`` is installed)
6. built-in defaults

Credentials are never written to ``settings.json``. They go to the OS keyring
when available, otherwise to ``credentials.json`` with ``0600`` permissions,
and they are redacted everywhere they could be logged or returned by the API.
"""

from __future__ import annotations

import json
import os
import platform
import re
import stat
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable

from .util import log
from .util.text import clean
from .version import APP_ID, APP_NAME

LOG = log.get("config")

try:  # pragma: no cover - optional
    import keyring as _keyring

    _HAVE_KEYRING = True
except Exception:  # pragma: no cover
    _keyring = None
    _HAVE_KEYRING = False


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """Directory containing the application (source tree or bundle)."""
    if _is_frozen():  # pragma: no cover - packaged build
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def user_data_dir() -> Path:
    """Per-user writable directory for the database, logs and exports."""
    override = os.environ.get("SCHEMATA_DATA_DIR")
    if override:
        return Path(override).expanduser()
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") \
            or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "schemata"


def user_config_dir() -> Path:
    override = os.environ.get("SCHEMATA_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    system = platform.system()
    if system == "Windows":
        return user_data_dir()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "schemata"


# --------------------------------------------------------------------------- #
# .env loading
# --------------------------------------------------------------------------- #

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def load_dotenv(paths: Iterable[Path] | None = None, override: bool = False
                ) -> dict[str, str]:
    """Load ``KEY=value`` pairs from .env files into ``os.environ``."""
    if paths is None:
        paths = [
            Path.cwd() / ".env",
            app_root() / ".env",
            user_config_dir() / ".env",
        ]
    loaded: dict[str, str] = {}
    for path in paths:
        try:
            if not path.is_file():
                continue
            for raw_line in path.read_text(encoding="utf-8",
                                           errors="replace").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                match = _ENV_LINE.match(line)
                if not match:
                    continue
                key, value = match.group(1), match.group(2).strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                value = value.split(" #")[0].strip() if not value.startswith("#") else value
                if override or key not in os.environ:
                    os.environ[key] = value
                loaded[key] = value
        except OSError:  # pragma: no cover
            continue
    return loaded


# --------------------------------------------------------------------------- #
# Provider credential descriptors
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CredentialField:
    key: str                  # internal name, e.g. "client_id"
    env: tuple[str, ...]      # accepted environment variable names
    label: str
    secret: bool = True
    required: bool = True
    help: str = ""


@dataclass(frozen=True)
class ProviderSpec:
    """Static description of a data provider: identity, docs and credentials."""

    id: str
    name: str
    kind: str                       # "distributor" | "aggregator" | "offline"
    credentials: tuple[CredentialField, ...] = ()
    signup_url: str = ""
    docs_url: str = ""
    default_rate: float = 5.0
    notes: str = ""
    regions: tuple[str, ...] = ("global",)
    supports_alternates: bool = False
    supports_compliance: bool = False
    enabled_by_default: bool = True

    @property
    def required_keys(self) -> tuple[str, ...]:
        return tuple(c.key for c in self.credentials if c.required)


PROVIDER_SPECS: dict[str, ProviderSpec] = {}


def _register(spec: ProviderSpec) -> ProviderSpec:
    PROVIDER_SPECS[spec.id] = spec
    return spec


MOCK = _register(ProviderSpec(
    id="mock", name="Offline Catalogue", kind="offline",
    notes="Deterministic built-in catalogue. Used when no keys are configured "
          "and for testing; never makes a network call.",
    default_rate=1000.0, supports_alternates=True, supports_compliance=True,
))

DIGIKEY = _register(ProviderSpec(
    id="digikey", name="DigiKey", kind="distributor",
    credentials=(
        CredentialField("client_id", ("DIGIKEY_CLIENT_ID", "BOMIQ_DIGIKEY_CLIENT_ID"),
                        "Client ID", secret=False,
                        help="From the DigiKey developer portal app."),
        CredentialField("client_secret",
                        ("DIGIKEY_CLIENT_SECRET", "BOMIQ_DIGIKEY_CLIENT_SECRET"),
                        "Client Secret"),
        CredentialField("customer_id", ("DIGIKEY_CUSTOMER_ID",), "Customer ID",
                        secret=False, required=False,
                        help="Optional: returns your contract pricing."),
    ),
    signup_url="https://developer.digikey.com/",
    docs_url="https://developer.digikey.com/products/product-information/productsearch/",
    default_rate=4.0, supports_compliance=True, supports_alternates=True,
    notes="OAuth2 client-credentials. Sandbox host available for testing.",
))

MOUSER = _register(ProviderSpec(
    id="mouser", name="Mouser Electronics", kind="distributor",
    credentials=(
        CredentialField("api_key", ("MOUSER_API_KEY", "BOMIQ_MOUSER_API_KEY"),
                        "Search API Key"),
    ),
    signup_url="https://www.mouser.com/api-hub/",
    docs_url="https://api.mouser.com/api/docs/ui/index",
    default_rate=1.5, supports_compliance=True,
    notes="Simple API key. Free tier is limited to ~1000 calls/day and 30 "
          "calls/minute, so Schemata throttles to 1.5 req/s.",
))

NEXAR = _register(ProviderSpec(
    id="nexar", name="Octopart / Nexar", kind="aggregator",
    credentials=(
        CredentialField("client_id", ("NEXAR_CLIENT_ID", "OCTOPART_CLIENT_ID"),
                        "Client ID", secret=False),
        CredentialField("client_secret",
                        ("NEXAR_CLIENT_SECRET", "OCTOPART_CLIENT_SECRET"),
                        "Client Secret"),
    ),
    signup_url="https://portal.nexar.com/",
    docs_url="https://api.nexar.com/",
    default_rate=2.0, supports_alternates=True, supports_compliance=True,
    notes="GraphQL aggregator covering ~150 distributors plus lifecycle and "
          "compliance data. The single most valuable provider for BOM risk.",
))

ARROW = _register(ProviderSpec(
    id="arrow", name="Arrow Electronics", kind="distributor",
    credentials=(
        CredentialField("api_key", ("ARROW_API_KEY",), "API Key"),
        CredentialField("login", ("ARROW_LOGIN",), "Login / e-mail",
                        secret=False, required=False),
    ),
    signup_url="https://developers.arrow.com/",
    docs_url="https://developers.arrow.com/api/index.php/site/page?view=itemService",
    default_rate=3.0, supports_compliance=True, enabled_by_default=False,
))

FARNELL = _register(ProviderSpec(
    id="farnell", name="Farnell / element14", kind="distributor",
    credentials=(
        CredentialField("api_key", ("FARNELL_API_KEY", "ELEMENT14_API_KEY"),
                        "API Key"),
        CredentialField("store", ("FARNELL_STORE",), "Store", secret=False,
                        required=False,
                        help="e.g. uk.farnell.com, in.element14.com, "
                             "sg.element14.com"),
    ),
    signup_url="https://partner.element14.com/",
    docs_url="https://partner.element14.com/docs/Product_Search_API_REST__Description",
    default_rate=3.0, regions=("emea", "apac"), enabled_by_default=False,
))

LCSC = _register(ProviderSpec(
    id="lcsc", name="LCSC Electronics", kind="distributor",
    credentials=(
        CredentialField("api_key", ("LCSC_API_KEY",), "API Key", required=False),
        CredentialField("api_secret", ("LCSC_API_SECRET",), "API Secret",
                        required=False),
    ),
    signup_url="https://www.lcsc.com/",
    docs_url="https://www.lcsc.com/api-guide",
    default_rate=2.0, regions=("apac",), enabled_by_default=False,
    notes="Low-cost APAC sourcing. Public endpoints work without a key but are "
          "rate limited and unofficial; supply a key when you have one.",
))

TRUSTEDPARTS = _register(ProviderSpec(
    id="trustedparts", name="TrustedParts", kind="aggregator",
    credentials=(
        CredentialField("api_key", ("TRUSTEDPARTS_API_KEY",), "API Key",
                        required=True),
    ),
    signup_url="https://www.trustedparts.com/",
    docs_url="https://www.trustedparts.com/en/about",
    default_rate=2.0, enabled_by_default=False,
    notes="Authorised-distributor stock aggregation; useful for scarce parts.",
))


# --------------------------------------------------------------------------- #
# Credentials store
# --------------------------------------------------------------------------- #

class Credentials:
    """Reads and writes provider credentials without ever logging them."""

    def __init__(self, config_dir: Path | None = None,
                 use_keyring: bool = True) -> None:
        self.config_dir = Path(config_dir or user_config_dir())
        self.file = self.config_dir / "credentials.json"
        self.use_keyring = use_keyring and _HAVE_KEYRING
        self._file_cache: dict[str, dict[str, str]] | None = None

    # -- file backend ----------------------------------------------------- #

    def _read_file(self) -> dict[str, dict[str, str]]:
        if self._file_cache is not None:
            return self._file_cache
        data: dict[str, dict[str, str]] = {}
        try:
            if self.file.is_file():
                raw = json.loads(self.file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = {
                        str(provider): {str(k): str(v) for k, v in values.items()}
                        for provider, values in raw.items()
                        if isinstance(values, dict)
                    }
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("Could not read credential file: %s", exc)
        self._file_cache = data
        return data

    def _write_file(self, data: dict[str, dict[str, str]]) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:  # pragma: no cover - Windows
            pass
        tmp.replace(self.file)
        self._file_cache = data

    # -- public API ------------------------------------------------------- #

    def get(self, provider: str, key: str) -> str:
        spec = PROVIDER_SPECS.get(provider)
        if spec:
            for credential in spec.credentials:
                if credential.key == key:
                    for env_name in credential.env:
                        value = clean(os.environ.get(env_name))
                        if value:
                            return value
                    break
        if self.use_keyring:  # pragma: no cover - environment dependent
            try:
                value = _keyring.get_password(APP_ID, f"{provider}.{key}")
                if value:
                    return value
            except Exception as exc:
                LOG.debug("Keyring read failed: %s", exc)
        return clean(self._read_file().get(provider, {}).get(key))

    def get_all(self, provider: str) -> dict[str, str]:
        spec = PROVIDER_SPECS.get(provider)
        keys = [c.key for c in spec.credentials] if spec else []
        keys += [k for k in self._read_file().get(provider, {}) if k not in keys]
        return {key: self.get(provider, key) for key in keys}

    def set(self, provider: str, values: dict[str, str]) -> None:
        stored_in_keyring: set[str] = set()
        if self.use_keyring:  # pragma: no cover
            for key, value in values.items():
                try:
                    if value:
                        _keyring.set_password(APP_ID, f"{provider}.{key}", value)
                    else:
                        try:
                            _keyring.delete_password(APP_ID, f"{provider}.{key}")
                        except Exception:
                            pass
                    stored_in_keyring.add(key)
                except Exception as exc:
                    LOG.debug("Keyring write failed for %s.%s: %s",
                              provider, key, exc)
        remaining = {k: v for k, v in values.items()
                     if k not in stored_in_keyring and v}
        data = dict(self._read_file())
        existing = dict(data.get(provider, {}))
        for key in values:
            existing.pop(key, None)
        existing.update(remaining)
        if existing:
            data[provider] = existing
        else:
            data.pop(provider, None)
        self._write_file(data)

    def clear(self, provider: str) -> None:
        spec = PROVIDER_SPECS.get(provider)
        keys = [c.key for c in spec.credentials] if spec else \
            list(self._read_file().get(provider, {}))
        self.set(provider, {key: "" for key in keys})

    def is_configured(self, provider: str) -> bool:
        spec = PROVIDER_SPECS.get(provider)
        if spec is None:
            return False
        if not spec.required_keys:
            return True
        values = self.get_all(provider)
        return all(clean(values.get(key)) for key in spec.required_keys)

    def status(self) -> dict[str, dict[str, Any]]:
        """Credential status, safe to serialise to the UI (no secret values)."""
        out: dict[str, dict[str, Any]] = {}
        for provider_id, spec in PROVIDER_SPECS.items():
            values = self.get_all(provider_id)
            out[provider_id] = {
                "configured": self.is_configured(provider_id),
                "fields": [
                    {
                        "key": credential.key,
                        "label": credential.label,
                        "secret": credential.secret,
                        "required": credential.required,
                        "help": credential.help,
                        "present": bool(clean(values.get(credential.key))),
                        "masked": _mask(values.get(credential.key, "")),
                        "from_env": any(os.environ.get(e)
                                        for e in credential.env),
                        "env_names": list(credential.env),
                    }
                    for credential in spec.credentials
                ],
                "backend": "keyring" if self.use_keyring else "file",
            }
        return out


#: lower and upper bounds for settings where an out-of-range value would
#: produce nonsense rather than just a different opinion
_SETTING_BOUNDS: dict[str, tuple[float, float]] = {
    "build_quantity": (1, 100_000_000),
    "lead_time_warn_days": (0, 3650),
    "lead_time_critical_days": (0, 3650),
    "stock_buffer_pct": (0, 1000),
    "min_sources_ok": (1, 20),
    "fuzzy_match_threshold": (0, 100),
    "review_confidence_threshold": (0, 100),
    "max_alternates_per_line": (0, 50),
    "max_workers": (1, 64),
    "request_timeout": (1, 600),
    "http_retries": (0, 10),
    "http_cache_ttl": (0, 30 * 86400),
    "part_cache_ttl": (0, 30 * 86400),
    "header_scan_rows": (1, 500),
    "port": (1, 65535),
    "weight_lifecycle": (0, 100),
    "weight_availability": (0, 100),
    "weight_sourcing": (0, 100),
    "weight_lead_time": (0, 100),
    "weight_compliance": (0, 100),
    "weight_data_quality": (0, 100),
    "weight_cost": (0, 100),
}


def _clamp_setting(key: str, value: Any, current: Any) -> Any:
    """Keep a setting inside a range where the engine still makes sense.

    A negative risk weight, a build quantity of zero or a worker count of -5
    are not opinions, they are broken configurations: they would make the
    weighted risk score divide by ~0 and produce garbage for every subsequent
    analysis. Out-of-range numbers are clamped rather than rejected, so the
    rest of a settings update still applies.
    """
    bounds = _SETTING_BOUNDS.get(key)
    if bounds is not None and isinstance(value, (int, float)) and \
            not isinstance(value, bool):
        low, high = bounds
        clamped = max(low, min(high, value))
        return type(current)(clamped) if isinstance(current, (int, float)) \
            and not isinstance(current, bool) else clamped
    if key == "currency":
        from .util.money import FALLBACK_RATES_PER_USD, normalize_currency

        code = normalize_currency(value, default="USD")
        return code if code in FALLBACK_RATES_PER_USD else current
    if key == "providers" and isinstance(value, list):
        known = [p for p in value if p in PROVIDER_SPECS]
        return known or current
    return value


def _mask(value: str) -> str:
    value = clean(value)
    if not value:
        return ""
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}{'*' * max(4, len(value) - 6)}{value[-3:]}"


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

@dataclass
class Settings:
    """Everything tunable about the engine, persisted as JSON."""

    # providers
    providers: list[str] = field(default_factory=lambda: [
        "nexar", "digikey", "mouser"])
    offline: bool = False
    allow_mock_fallback: bool = True
    digikey_sandbox: bool = False

    # analysis
    build_quantity: int = 100
    currency: str = "USD"
    target_region: str = "global"
    lead_time_warn_days: int = 84            # 12 weeks
    lead_time_critical_days: int = 168       # 24 weeks
    stock_buffer_pct: float = 10.0
    min_sources_ok: int = 2
    fuzzy_match_threshold: int = 88
    review_confidence_threshold: int = 90
    max_alternates_per_line: int = 5
    prefer_authorized_only: bool = True
    include_non_stocking_offers: bool = False
    auto_complete_fields: bool = True
    auto_apply_completions: bool = False
    require_rohs: bool = True
    require_reach: bool = False
    flag_export_controlled: bool = True

    # risk weights (relative; normalised at scoring time)
    weight_lifecycle: float = 1.0
    weight_availability: float = 1.0
    weight_sourcing: float = 0.8
    weight_lead_time: float = 0.7
    weight_compliance: float = 0.6
    weight_data_quality: float = 0.5
    weight_cost: float = 0.4

    # performance
    max_workers: int = 8
    request_timeout: float = 25.0
    http_retries: int = 3
    cache_enabled: bool = True
    http_cache_ttl: int = 21600               # 6 h
    part_cache_ttl: int = 21600
    rate_limit_overrides: dict[str, float] = field(default_factory=dict)

    # ingestion
    header_scan_rows: int = 40
    multi_sheet: bool = True
    expand_ref_ranges: bool = True
    merge_duplicate_mpns: bool = True
    treat_blank_qty_as_one: bool = True
    learn_templates: bool = True

    # ui / server
    host: str = "127.0.0.1"
    port: int = 8756
    open_browser: bool = True
    theme: str = "dark"
    log_level: str = "INFO"

    @classmethod
    def field_names(cls) -> set[str]:
        return {f.name for f in fields(cls)}

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def update(self, values: dict[str, Any]) -> list[str]:
        """Apply a partial update, coercing types. Returns changed keys."""
        changed: list[str] = []
        types = {f.name: f.type for f in fields(self)}
        for key, value in (values or {}).items():
            if key not in types:
                continue
            current = getattr(self, key)
            try:
                if isinstance(current, bool):
                    new = value if isinstance(value, bool) else \
                        clean(value).lower() in ("1", "true", "yes", "on")
                elif isinstance(current, int) and not isinstance(current, bool):
                    new = int(float(value))
                elif isinstance(current, float):
                    new = float(value)
                elif isinstance(current, list):
                    new = list(value) if isinstance(value, (list, tuple)) else \
                        [p.strip() for p in str(value).split(",") if p.strip()]
                elif isinstance(current, dict):
                    new = dict(value) if isinstance(value, dict) else current
                else:
                    new = clean(value)
            except (TypeError, ValueError):
                continue
            new = _clamp_setting(key, new, current)
            if new != current:
                setattr(self, key, new)
                changed.append(key)
        return changed


class Config:
    """Bundle of settings, credentials and resolved paths."""

    def __init__(self, data_dir: Path | None = None,
                 config_dir: Path | None = None,
                 load_env: bool = True, use_keyring: bool = True) -> None:
        if load_env:
            load_dotenv()
        self.data_dir = Path(data_dir or user_data_dir())
        self.config_dir = Path(config_dir or user_config_dir())
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.settings_file = self.config_dir / "settings.json"
        self.settings = Settings()
        self._load_settings()
        self._apply_env_overrides()
        self.credentials = Credentials(self.config_dir, use_keyring=use_keyring)

    # -- paths ------------------------------------------------------------ #

    @property
    def db_path(self) -> Path:
        return self.data_dir / "schemata_bom.sqlite3"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def export_dir(self) -> Path:
        path = self.data_dir / "exports"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def upload_dir(self) -> Path:
        path = self.data_dir / "uploads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- settings persistence --------------------------------------------- #

    def _load_settings(self) -> None:
        try:
            if self.settings_file.is_file():
                raw = json.loads(self.settings_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.settings.update(raw)
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("settings.json could not be read (%s); using defaults.", exc)

    def _apply_env_overrides(self) -> None:
        overrides: dict[str, Any] = {}
        for name in Settings.field_names():
            env_value = os.environ.get(f"BOMIQ_{name.upper()}")
            if env_value is not None:
                overrides[name] = env_value
        if os.environ.get("BOMIQ_OFFLINE") in ("1", "true", "yes"):
            overrides["offline"] = True
        if overrides:
            self.settings.update(overrides)

    def save(self) -> None:
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.settings_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.settings.to_dict(), indent=2),
                           encoding="utf-8")
            tmp.replace(self.settings_file)
        except OSError as exc:  # pragma: no cover
            LOG.error("Could not save settings: %s", exc)

    # -- provider helpers ------------------------------------------------- #

    def enabled_providers(self) -> list[str]:
        """Providers that are both selected and usable right now."""
        if self.settings.offline:
            return ["mock"]
        usable = [
            provider_id for provider_id in self.settings.providers
            if provider_id in PROVIDER_SPECS
            and (provider_id == "mock"
                 or self.credentials.is_configured(provider_id))
        ]
        if not usable and self.settings.allow_mock_fallback:
            return ["mock"]
        return usable

    def provider_rate(self, provider_id: str) -> float:
        override = self.settings.rate_limit_overrides.get(provider_id)
        if override:
            try:
                return float(override)
            except (TypeError, ValueError):
                pass
        spec = PROVIDER_SPECS.get(provider_id)
        return spec.default_rate if spec else 3.0

    def describe_providers(self) -> list[dict[str, Any]]:
        status = self.credentials.status()
        out = []
        for provider_id, spec in PROVIDER_SPECS.items():
            out.append({
                "id": spec.id,
                "name": spec.name,
                "kind": spec.kind,
                "notes": spec.notes,
                "signup_url": spec.signup_url,
                "docs_url": spec.docs_url,
                "regions": list(spec.regions),
                "supports_alternates": spec.supports_alternates,
                "supports_compliance": spec.supports_compliance,
                "selected": provider_id in self.settings.providers,
                "active": provider_id in self.enabled_providers(),
                "rate_per_second": self.provider_rate(provider_id),
                **status.get(provider_id, {}),
            })
        return out

    def summary(self) -> dict[str, Any]:
        return {
            "data_dir": str(self.data_dir),
            "config_dir": str(self.config_dir),
            "db_path": str(self.db_path),
            "offline": self.settings.offline,
            "enabled_providers": self.enabled_providers(),
            "keyring": _HAVE_KEYRING,
            "frozen": _is_frozen(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
        }


_default_config: Config | None = None


def get_config(**kwargs: Any) -> Config:
    """Process-wide default :class:`Config` (created on first use)."""
    global _default_config
    if _default_config is None:
        _default_config = Config(**kwargs)
    return _default_config


def reset_config() -> None:
    """Testing hook."""
    global _default_config
    _default_config = None
