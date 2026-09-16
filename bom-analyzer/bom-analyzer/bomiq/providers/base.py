"""
Provider abstraction.

A provider turns a part number (plus optional manufacturer) into a
:class:`~bomiq.core.models.PartData`. Every provider gets, for free:

* per-provider token-bucket rate limiting and a circuit breaker
* retry with jittered backoff and ``Retry-After`` support
* a two-level cache (raw HTTP responses and normalised part records)
* credential checking with a clear message when keys are missing
* statistics (calls, hits, misses, errors, latency) surfaced in the report

Providers are synchronous; concurrency is handled by the registry's thread
pool. That keeps each provider simple and easy to reason about, and matches
what the distributor APIs actually reward (a small number of steady threads).
"""

from __future__ import annotations

import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from ..config import PROVIDER_SPECS, Config, ProviderSpec
from ..core.errors import CredentialError, ProviderError
from ..core.models import (
    Compliance, ComplianceState, Lifecycle, Offer, PartData, PriceBreak,
)
from ..util import log
from ..util.http import CircuitOpen, HttpClient, HttpError
from ..util.money import normalize_currency, to_decimal
from ..util.text import (
    clean, manufacturer_key, normalize_distributor, normalize_manufacturer,
    normalize_mpn,
)
from ..util.units import mount_type, normalize_package, parse_int, parse_lead_time_days

LOG = log.get("providers.base")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Vocabulary normalisation
# --------------------------------------------------------------------------- #

_LIFECYCLE_MAP: list[tuple[tuple[str, ...], Lifecycle]] = [
    (("obsolete", "discontinued", "cancelled", "canceled", "inactive",
      "not manufactured", "no longer", "end of sale", "eos", "dead",
      "unavailable - obsolete", "phase out complete"), Lifecycle.OBSOLETE),
    (("end of life", "eol", "last time buy", "ltb", "last buy",
      "end-of-life", "phase out", "phasing out", "pending obsolescence",
      "scheduled for obsolescence"), Lifecycle.EOL),
    (("nrnd", "not recommended", "not for new design",
      "not recommended for new design", "not recommended for new designs",
      "mature", "legacy", "limited availability", "maintenance"),
     Lifecycle.NRND),
    (("new", "new product", "preliminary", "pre-production", "prerelease",
      "pre-release", "introduced", "recently introduced", "sampling"),
     Lifecycle.NEW),
    (("active", "production", "in production", "available", "released",
      "volume production", "normal", "standard", "current", "mass production",
      "recommended", "unknown active", "acquired"), Lifecycle.ACTIVE),
]


def normalize_lifecycle(*values: object) -> tuple[Lifecycle, str]:
    """Map any distributor's lifecycle wording onto :class:`Lifecycle`.

    The *worst* state wins when several sources disagree, because obsolescence
    reported by one distributor is real even if another still shows Active.

    >>> normalize_lifecycle("Not Recommended for New Designs")[0]
    <Lifecycle.NRND: 'NRND'>
    """
    found: list[tuple[Lifecycle, str]] = []
    for value in values:
        text = clean(value).lower()
        if not text:
            continue
        for needles, state in _LIFECYCLE_MAP:
            if any(needle in text for needle in needles):
                found.append((state, clean(value)))
                break
    if not found:
        return Lifecycle.UNKNOWN, ""
    worst = max(found, key=lambda item: item[0].rank)
    return worst[0], worst[1]


# Phrases are matched as substrings; single words are matched on word
# boundaries. Mixing the two is what makes "ROHS3 Compliant" read correctly --
# a naive substring search for "n" finds one inside "compliant".
_EXEMPT_PHRASES = ("by exemption", "with exemption", "exemption applies")
_EXEMPT_WORDS = ("exempt", "exemption", "exempted")

_NON_COMPLIANT_PHRASES = ("non-compliant", "non compliant", "not compliant",
                          "non_compliant", "noncompliant", "not rohs",
                          "non-rohs", "non rohs", "does not comply",
                          "not in compliance", "contains svhc",
                          "above threshold")
_NON_COMPLIANT_WORDS = ("no", "n", "false", "fail", "failed", "contains",
                        "noncompliant")

_UNKNOWN_PHRASES = ("not applicable", "not available", "no information",
                    "not determined", "to be determined", "not provided",
                    "not reported", "request", "under review")
_UNKNOWN_WORDS = ("unknown", "n/a", "na", "tbd", "pending", "undetermined",
                  "unspecified", "unstated")

_COMPLIANT_PHRASES = ("rohs 3", "rohs3", "lead free", "lead-free", "pb free",
                      "pb-free", "in compliance", "fully compliant",
                      "rohs compliant", "reach compliant", "svhc free",
                      "no svhc", "below threshold")
_COMPLIANT_WORDS = ("compliant", "yes", "y", "true", "conform", "conforms",
                    "pass", "passed", "compatible", "compliance", "green",
                    "ok")

_WORD_RE = re.compile(r"[a-z0-9/]+")


def normalize_compliance_state(value: object) -> ComplianceState:
    """Map a free-text compliance string onto :class:`ComplianceState`.

    Order matters: exemptions and explicit non-compliance are decided before
    the generic "compliant" vocabulary, so ``RoHS non-compliant`` is never read
    as compliant.

    >>> normalize_compliance_state("ROHS3 Compliant")
    <ComplianceState.COMPLIANT: 'Compliant'>
    >>> normalize_compliance_state("RoHS non-compliant")
    <ComplianceState.NON_COMPLIANT: 'Non-compliant'>
    """
    text = clean(value).lower()
    if not text:
        return ComplianceState.UNKNOWN
    words = set(_WORD_RE.findall(text))

    def has(phrases: tuple[str, ...], single: tuple[str, ...]) -> bool:
        if any(phrase in text for phrase in phrases):
            return True
        return bool(words & set(single))

    if has(_EXEMPT_PHRASES, _EXEMPT_WORDS):
        return ComplianceState.EXEMPT
    if has(_NON_COMPLIANT_PHRASES, _NON_COMPLIANT_WORDS):
        # "no SVHC" and "no exemptions" are statements of compliance.
        if not any(phrase in text for phrase in
                   ("no exemption", "no svhc", "no restricted")):
            return ComplianceState.NON_COMPLIANT
    if has(_COMPLIANT_PHRASES, _COMPLIANT_WORDS):
        return ComplianceState.COMPLIANT
    if has(_UNKNOWN_PHRASES, _UNKNOWN_WORDS):
        return ComplianceState.UNKNOWN
    return ComplianceState.UNKNOWN


def build_price_breaks(raw: Iterable[Any], currency: str = "USD",
                       qty_keys: Sequence[str] = ("BreakQuantity", "Quantity",
                                                  "quantity", "qty", "minQty",
                                                  "MinimumQuantity", "moq"),
                       price_keys: Sequence[str] = ("UnitPrice", "Price",
                                                    "price", "unitPrice",
                                                    "cost", "Cost")
                       ) -> list[PriceBreak]:
    """Normalise a provider's price ladder into :class:`PriceBreak` objects."""
    breaks: list[PriceBreak] = []
    for entry in raw or []:
        if isinstance(entry, dict):
            quantity = None
            price = None
            for key in qty_keys:
                if key in entry and entry[key] is not None:
                    quantity = parse_int(entry[key])
                    break
            for key in price_keys:
                if key in entry and entry[key] is not None:
                    price = to_decimal(entry[key])
                    break
            entry_currency = normalize_currency(
                entry.get("Currency") or entry.get("currency") or currency,
                default=currency)
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            quantity, price = parse_int(entry[0]), to_decimal(entry[1])
            entry_currency = currency
        else:
            continue
        if quantity is None or quantity < 1:
            quantity = 1
        if price is None or price <= 0:
            # A zero price means "call for pricing" or a data error, never a
            # free part. Letting it through would cost the line at 0.00 and
            # report it as fully costed.
            continue
        breaks.append(PriceBreak(quantity=quantity, unit_price=price,
                                 currency=entry_currency))
    # Collapse duplicates, keep the cheapest per quantity, sort ascending.
    by_quantity: dict[int, PriceBreak] = {}
    for item in breaks:
        existing = by_quantity.get(item.quantity)
        if existing is None or item.unit_price < existing.unit_price:
            by_quantity[item.quantity] = item
    return [by_quantity[q] for q in sorted(by_quantity)]


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #

@dataclass
class ProviderStats:
    calls: int = 0
    hits: int = 0
    misses: int = 0
    cache_hits: int = 0
    errors: int = 0
    auth_errors: int = 0
    rate_limited: int = 0
    total_ms: int = 0
    last_error: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, *, hit: bool = False, miss: bool = False,
               cached: bool = False, error: str = "", ms: int = 0,
               auth: bool = False, throttled: bool = False) -> None:
        with self._lock:
            if cached:
                self.cache_hits += 1
            else:
                self.calls += 1
            if hit:
                self.hits += 1
            if miss:
                self.misses += 1
            if error:
                self.errors += 1
                self.last_error = error[:400]
            if auth:
                self.auth_errors += 1
            if throttled:
                self.rate_limited += 1
            self.total_ms += ms

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            average = int(self.total_ms / self.calls) if self.calls else 0
            return {
                "calls": self.calls, "hits": self.hits, "misses": self.misses,
                "cache_hits": self.cache_hits, "errors": self.errors,
                "auth_errors": self.auth_errors,
                "rate_limited": self.rate_limited,
                "avg_latency_ms": average,
                "hit_rate_pct": round(
                    100.0 * self.hits / max(1, self.hits + self.misses), 1),
                "last_error": self.last_error,
            }


# --------------------------------------------------------------------------- #
# Base provider
# --------------------------------------------------------------------------- #

class Provider(ABC):
    """Base class for every data source."""

    #: provider id, must match a key in :data:`bomiq.config.PROVIDER_SPECS`
    id: str = ""
    #: currency the API returns unless told otherwise
    default_currency: str = "USD"
    #: whether this provider can suggest alternates
    can_suggest_alternates: bool = False

    def __init__(self, config: Config, http: HttpClient | None = None,
                 part_cache: Any | None = None) -> None:
        self.config = config
        self.spec: ProviderSpec = PROVIDER_SPECS[self.id]
        self.stats = ProviderStats()
        self.part_cache = part_cache
        self.http = http or HttpClient(
            rate_per_second=config.provider_rate(self.id),
            retries=config.settings.http_retries,
            timeout=config.settings.request_timeout,
        )
        self._auth_lock = threading.Lock()
        self._disabled_reason = ""

    # -- identity --------------------------------------------------------- #

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def available(self) -> bool:
        return not self._disabled_reason

    @property
    def disabled_reason(self) -> str:
        return self._disabled_reason

    def disable(self, reason: str) -> None:
        """Take the provider out of service for the rest of the run."""
        if not self._disabled_reason:
            self._disabled_reason = reason
            LOG.warning("%s disabled for this run: %s", self.name, reason)

    def credential(self, key: str, required: bool = True) -> str:
        value = self.config.credentials.get(self.id, key)
        if not value and required:
            raise CredentialError(
                f"{self.name} is enabled but the {key.replace('_', ' ')} is "
                f"not set. Add it in Settings → Providers, or set the "
                f"environment variable.",
                provider=self.id, key=key,
            )
        return value

    def check_credentials(self) -> tuple[bool, str]:
        """Verify configuration without making a network call."""
        missing = [
            credential.label for credential in self.spec.credentials
            if credential.required
            and not self.config.credentials.get(self.id, credential.key)
        ]
        if missing:
            return False, f"Missing: {', '.join(missing)}"
        return True, "Credentials present"

    def self_test(self) -> dict[str, Any]:
        """Optional live check used by the Settings screen's *Test* button."""
        ok, message = self.check_credentials()
        if not ok:
            return {"provider": self.id, "ok": False, "message": message}
        started = time.monotonic()
        try:
            part = self.lookup(self.test_mpn(), use_cache=False)
        except (ProviderError, HttpError, CredentialError) as exc:
            return {"provider": self.id, "ok": False, "message": str(exc)}
        elapsed = int((time.monotonic() - started) * 1000)
        return {
            "provider": self.id,
            "ok": part is not None,
            "message": (f"Found {part.mpn} with {len(part.offers)} offer(s)"
                        if part else
                        "Connected, but the test part was not found "
                        "(this is usually fine)"),
            "latency_ms": elapsed,
        }

    def test_mpn(self) -> str:
        """A part every catalogue carries, used by :meth:`self_test`."""
        return "RC0603FR-0710KL"

    # -- lookup ----------------------------------------------------------- #

    def lookup(self, mpn: str, manufacturer: str = "",
               use_cache: bool = True) -> PartData | None:
        """Cached, error-tolerant single-part lookup.

        Returns ``None`` when the part is genuinely not in the catalogue.
        Raises :class:`ProviderError` only for infrastructure problems, so the
        engine can distinguish "no such part" from "could not ask".
        """
        term = clean(mpn)
        if not term:
            return None
        if not self.available:
            raise ProviderError(self.id, self._disabled_reason, retryable=False)

        mpn_key = normalize_mpn(term)
        mfr_key = manufacturer_key(manufacturer)
        if use_cache and self.part_cache is not None:
            cached = self.part_cache.get(self.id, mpn_key, mfr_key)
            if cached is not None:
                self.stats.record(cached=True, hit=bool(cached.get("mpn")))
                return PartData.from_dict(cached) if cached.get("mpn") else None

        started = time.monotonic()
        try:
            part = self.fetch(term, manufacturer)
        except CredentialError as exc:
            self.stats.record(error=str(exc), auth=True)
            self.disable(str(exc))
            raise ProviderError(self.id, str(exc), retryable=False) from exc
        except CircuitOpen as exc:
            self.stats.record(error=str(exc))
            raise ProviderError(self.id, str(exc)) from exc
        except HttpError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            self.stats.record(error=str(exc), ms=elapsed,
                              auth=exc.is_auth_error,
                              throttled=exc.is_rate_limit)
            if exc.is_auth_error:
                self.disable(
                    f"{self.name} rejected the credentials (HTTP "
                    f"{exc.status}). Check the keys in Settings.")
            raise ProviderError(self.id, str(exc),
                                retryable=not exc.is_auth_error) from exc
        except ProviderError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            elapsed = int((time.monotonic() - started) * 1000)
            self.stats.record(error=repr(exc), ms=elapsed)
            raise ProviderError(
                self.id, f"{self.name} returned data BOM-IQ could not read: "
                         f"{exc}") from exc

        elapsed = int((time.monotonic() - started) * 1000)
        self.stats.record(hit=part is not None, miss=part is None, ms=elapsed)

        if part is not None:
            part.providers = sorted(set(part.providers) | {self.id})
            part.fetched_at = utc_now()
            self._finalise(part)
        if self.part_cache is not None:
            payload = part.to_dict() if part else {"mpn": ""}
            self.part_cache.set(self.id, mpn_key, payload, mfr_key,
                                ttl=self.config.settings.part_cache_ttl)
        return part

    @abstractmethod
    def fetch(self, mpn: str, manufacturer: str = "") -> PartData | None:
        """Provider-specific lookup. Implementations should not catch
        :class:`HttpError` -- the base class handles it."""

    def search(self, query: str, limit: int = 10) -> list[PartData]:
        """Keyword / parametric search. Optional; default is empty."""
        return []

    def alternates(self, part: PartData, limit: int = 10) -> list[PartData]:
        """Substitutes for a part. Optional; default is empty."""
        return []

    # -- helpers for subclasses ------------------------------------------- #

    def _finalise(self, part: PartData) -> None:
        """Tidy up a freshly-built record: canonical names, derived fields."""
        part.mpn = clean(part.mpn)
        part.manufacturer = normalize_manufacturer(part.manufacturer)
        part.package = part.package or ""
        if part.package:
            normalised = normalize_package(part.package)
            part.specs.setdefault("Package (as supplied)", part.package)
            part.package = normalised or part.package
        if not part.mount:
            part.mount = mount_type(part.package)
        if part.total_avail is None:
            part.total_avail = part.total_stock or None
        # Deduplicate offers per (canonical distributor, sku).
        unique: dict[tuple[str, str], Offer] = {}
        for offer in part.offers:
            offer.distributor = normalize_distributor(offer.distributor) \
                or offer.distributor
            key = (offer.distributor.lower(), offer.sku.lower())
            existing = unique.get(key)
            if existing is None or (offer.stock or 0) > (existing.stock or 0):
                unique[key] = offer
        part.offers = list(unique.values())
        for offer in part.offers:
            offer.currency = normalize_currency(offer.currency,
                                                default=self.default_currency)
            if offer.lead_time_days is None and offer.in_stock:
                offer.lead_time_days = 0
        part.alternate_mpns = _dedupe_keep_order(part.alternate_mpns)
        part.similar_mpns = _dedupe_keep_order(part.similar_mpns)

    def make_offer(self, *, distributor: str | None = None, **kwargs: Any) -> Offer:
        return Offer(provider=self.id, distributor=distributor or self.name,
                     **kwargs)

    @staticmethod
    def compliance_from_specs(specs: dict[str, str]) -> Compliance:
        """Pull compliance facts out of a provider's parametric block."""
        compliance = Compliance()
        for key, value in specs.items():
            lowered = key.lower()
            if "rohs" in lowered and compliance.rohs is ComplianceState.UNKNOWN:
                compliance.rohs = normalize_compliance_state(value)
                compliance.rohs_note = clean(value)
            elif "reach" in lowered and compliance.reach is ComplianceState.UNKNOWN:
                compliance.reach = normalize_compliance_state(value)
                compliance.reach_note = clean(value)
            elif "halogen" in lowered:
                compliance.halogen_free = normalize_compliance_state(value)
            elif "moisture sensitivity" in lowered or lowered == "msl":
                compliance.msl = clean(value)
            elif "automotive" in lowered or "aec" in lowered:
                compliance.aec_q = clean(value)
            elif "country of origin" in lowered or lowered in ("coo", "origin"):
                compliance.country_of_origin = clean(value)
            elif "htsus" in lowered or "hts" in lowered or "tariff" in lowered:
                compliance.hts_code = clean(value)
            elif "eccn" in lowered or "export control" in lowered:
                compliance.eccn = clean(value)
            elif "lead free" in lowered or "lead-free" in lowered:
                compliance.lead_free_process = clean(value)
        return compliance

    @staticmethod
    def lead_days(*values: object) -> int | None:
        for value in values:
            days = parse_lead_time_days(value)
            if days is not None:
                return days
        return None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} id={self.id!r}>"


def _dedupe_keep_order(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = clean(value)
        if not text:
            continue
        key = text.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def merge_parts(parts: Sequence[PartData]) -> PartData | None:
    """Combine records for the same part from several providers.

    Rules: worst lifecycle wins; offers are concatenated; text fields are taken
    from the first provider that supplied a non-empty value; compliance takes
    the most definite answer available.
    """
    parts = [part for part in parts if part is not None]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]

    primary = max(parts, key=lambda part: (len(part.offers), len(part.specs)))
    merged = PartData(mpn=primary.mpn, manufacturer=primary.manufacturer)

    for field_name in ("description", "category", "series", "package", "mount",
                       "datasheet_url", "image_url", "product_url"):
        for part in [primary] + [p for p in parts if p is not primary]:
            value = clean(getattr(part, field_name))
            if value:
                setattr(merged, field_name, value)
                break

    lifecycles = [(part.lifecycle, part.lifecycle_note) for part in parts]
    worst = max(lifecycles, key=lambda item: item[0].rank)
    merged.lifecycle, merged.lifecycle_note = worst
    for part in parts:
        for provider in part.providers:
            if part.lifecycle is not Lifecycle.UNKNOWN:
                merged.lifecycle_sources[provider] = part.lifecycle.value

    for part in parts:
        merged.offers.extend(part.offers)
        merged.providers.extend(part.providers)
        merged.alternate_mpns.extend(part.alternate_mpns)
        merged.similar_mpns.extend(part.similar_mpns)
        for key, value in part.specs.items():
            merged.specs.setdefault(key, value)

    merged.providers = sorted(set(merged.providers))
    merged.alternate_mpns = _dedupe_keep_order(merged.alternate_mpns)
    merged.similar_mpns = _dedupe_keep_order(merged.similar_mpns)

    # Offers: one entry per (canonical distributor, sku). Distributor names
    # are canonicalised first, because an aggregator calls DigiKey "Digi-Key"
    # and without this the same inventory is counted twice -- which would make
    # a single-sourced part read as comfortably multi-sourced.
    unique: dict[tuple[str, str], Offer] = {}
    for offer in merged.offers:
        offer.distributor = normalize_distributor(offer.distributor) \
            or offer.distributor
        key = (offer.distributor.lower(), offer.sku.lower())
        existing = unique.get(key)
        if existing is None or (offer.stock or 0) > (existing.stock or 0):
            unique[key] = offer

    # A direct provider and an aggregator often carry the *same* distributor
    # with different SKUs (packaging variants). Summing their stock would
    # double-count it, so per distributor only the single richest offer's
    # stock is kept; the other offers are retained for their price ladders but
    # contribute no stock.
    by_distributor: dict[str, list[Offer]] = {}
    for offer in unique.values():
        by_distributor.setdefault(offer.distributor.lower(), []).append(offer)
    for offers in by_distributor.values():
        if len(offers) < 2:
            continue
        providers_seen = {offer.provider for offer in offers}
        if len(providers_seen) < 2:
            continue      # same provider: genuinely distinct packaging SKUs
        keeper = max(offers, key=lambda o: ((o.stock or 0),
                                            len(o.price_breaks)))
        for offer in offers:
            if offer is keeper:
                continue
            offer.stock = 0
            offer.warnings = list(offer.warnings) + [
                f"Stock counted once under {keeper.distributor} "
                f"(also reported via {offer.provider})"]

    merged.offers = sorted(unique.values(),
                           key=lambda o: (-(o.stock or 0), o.distributor))

    merged.compliance = _merge_compliance([part.compliance for part in parts])
    merged.total_avail = merged.total_stock or None
    lead_times = [part.estimated_factory_lead_days for part in parts
                  if part.estimated_factory_lead_days]
    merged.estimated_factory_lead_days = max(lead_times) if lead_times else None
    priced = [part for part in parts if part.median_price_1k]
    if priced:
        cheapest = min(priced, key=lambda part: part.median_price_1k)
        merged.median_price_1k = cheapest.median_price_1k
        merged.median_price_1k_currency = \
            cheapest.median_price_1k_currency or "USD"
    merged.fetched_at = max((part.fetched_at for part in parts), default="")
    return merged


_STATE_PRIORITY = {
    ComplianceState.NON_COMPLIANT: 3,
    ComplianceState.EXEMPT: 2,
    ComplianceState.COMPLIANT: 1,
    ComplianceState.UNKNOWN: 0,
}


def _merge_compliance(items: Sequence[Compliance]) -> Compliance:
    out = Compliance()
    for attribute in ("rohs", "reach", "halogen_free"):
        best = ComplianceState.UNKNOWN
        note = ""
        for item in items:
            state = getattr(item, attribute)
            if _STATE_PRIORITY[state] > _STATE_PRIORITY[best]:
                best = state
                note = getattr(item, f"{attribute}_note", "") if attribute != \
                    "halogen_free" else ""
        setattr(out, attribute, best)
        if attribute == "rohs":
            out.rohs_note = note
        elif attribute == "reach":
            out.reach_note = note
    for attribute in ("country_of_origin", "hts_code", "eccn", "msl", "aec_q",
                      "conflict_minerals", "lead_free_process"):
        for item in items:
            value = clean(getattr(item, attribute))
            if value:
                setattr(out, attribute, value)
                break
    svhc: list[str] = []
    for item in items:
        svhc.extend(item.svhc)
    out.svhc = _dedupe_keep_order(svhc)
    for item in items:
        if item.export_controlled is not None and out.export_controlled is None:
            out.export_controlled = item.export_controlled
        if item.itar is not None and out.itar is None:
            out.itar = item.itar
        out.sources.extend(item.sources)
    out.sources = _dedupe_keep_order(out.sources)
    return out
