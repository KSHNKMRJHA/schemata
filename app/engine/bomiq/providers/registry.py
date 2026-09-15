"""
Provider registry and the concurrent fan-out that feeds the analysis engine.

Responsibilities
----------------
* build provider instances from configuration (and nothing else)
* share one :class:`~bomiq.util.http.HttpClient` so rate limits and the
  circuit breaker are global per host
* look a part up across every enabled provider in parallel, then merge the
  records into one consolidated :class:`PartData`
* use a provider's batch endpoint when it has one (Nexar) instead of N calls
* never let one provider's failure stop the run: errors are collected per part
  and reported, and a provider that keeps failing is dropped for the rest of
  the analysis
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterable, Sequence

from ..config import PROVIDER_SPECS, Config
from ..core.errors import ProviderError
from ..core.models import PartData
from ..util import log
from ..util.http import HttpClient
from ..util.text import clean, manufacturer_key, normalize_mpn
from .arrow import ArrowProvider
from .base import Provider, merge_parts
from .digikey import DigiKeyProvider
from .farnell import FarnellProvider
from .lcsc import LcscProvider
from .mock import MockProvider
from .mouser import MouserProvider
from .nexar import NexarProvider
from .trustedparts import TrustedPartsProvider

LOG = log.get("providers.registry")

PROVIDER_CLASSES: dict[str, type[Provider]] = {
    "mock": MockProvider,
    "digikey": DigiKeyProvider,
    "mouser": MouserProvider,
    "nexar": NexarProvider,
    "arrow": ArrowProvider,
    "farnell": FarnellProvider,
    "lcsc": LcscProvider,
    "trustedparts": TrustedPartsProvider,
}

#: providers ordered by how much they contribute to risk analysis; used when
#: deciding which record's text fields win a merge
PROVIDER_PRIORITY = ["nexar", "digikey", "mouser", "arrow", "farnell",
                     "trustedparts", "lcsc", "mock"]


class LookupResult:
    """Consolidated answer for one part, plus what went wrong where."""

    __slots__ = ("part", "per_provider", "errors", "providers_tried")

    def __init__(self) -> None:
        self.part: PartData | None = None
        self.per_provider: dict[str, PartData] = {}
        self.errors: dict[str, str] = {}
        self.providers_tried: list[str] = []

    @property
    def found(self) -> bool:
        return self.part is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "part": self.part.to_dict() if self.part else None,
            "providers": {k: v.to_dict() for k, v in self.per_provider.items()},
            "errors": dict(self.errors),
            "providers_tried": list(self.providers_tried),
        }


class ProviderRegistry:
    """Owns the provider instances for one analysis run."""

    def __init__(self, config: Config, part_cache: Any | None = None,
                 http: HttpClient | None = None,
                 provider_ids: Sequence[str] | None = None) -> None:
        self.config = config
        self.part_cache = part_cache
        self.http = http or HttpClient(
            rate_per_second=4.0,
            retries=config.settings.http_retries,
            timeout=config.settings.request_timeout,
        )
        # Start from what the user *selected*, not from what is already usable,
        # so a provider that was skipped for a missing key is recorded in
        # ``setup_errors`` and surfaced in the report instead of silently
        # vanishing.
        if provider_ids is not None:
            wanted = list(provider_ids)
        elif config.settings.offline:
            wanted = ["mock"]
        else:
            wanted = list(config.settings.providers) or ["mock"]
        self.providers: dict[str, Provider] = {}
        self.setup_errors: dict[str, str] = {}
        self._lock = threading.Lock()

        for provider_id in wanted:
            if provider_id not in PROVIDER_CLASSES:
                self.setup_errors[provider_id] = "Unknown provider"
                continue
            try:
                provider = PROVIDER_CLASSES[provider_id](
                    config, http=self.http, part_cache=part_cache)
                ok, message = provider.check_credentials()
                if not ok:
                    self.setup_errors[provider_id] = message
                    LOG.info("Skipping %s: %s", provider_id, message)
                    continue
                spec = PROVIDER_SPECS[provider_id]
                self.http.set_rate(
                    _host_for(provider_id),
                    config.provider_rate(provider_id) or spec.default_rate)
                self.providers[provider_id] = provider
            except Exception as exc:  # pragma: no cover - defensive
                self.setup_errors[provider_id] = str(exc)
                LOG.warning("Could not initialise %s: %s", provider_id, exc)

        if not self.providers and config.settings.allow_mock_fallback:
            LOG.info("No live provider is usable; falling back to the offline "
                     "catalogue.")
            self.providers["mock"] = MockProvider(config, part_cache=None)

    # -- introspection ---------------------------------------------------- #

    @property
    def ids(self) -> list[str]:
        return [
            pid for pid in PROVIDER_PRIORITY if pid in self.providers
        ] + [pid for pid in self.providers if pid not in PROVIDER_PRIORITY]

    @property
    def is_offline(self) -> bool:
        return set(self.providers) <= {"mock"}

    def active(self) -> list[Provider]:
        return [self.providers[pid] for pid in self.ids
                if self.providers[pid].available]

    def stats(self) -> dict[str, dict[str, Any]]:
        out = {pid: provider.stats.to_dict()
               for pid, provider in self.providers.items()}
        for pid, provider in self.providers.items():
            out[pid]["name"] = provider.name
            out[pid]["available"] = provider.available
            out[pid]["disabled_reason"] = provider.disabled_reason
        for pid, message in self.setup_errors.items():
            out.setdefault(pid, {})["skipped"] = message
        out["_http"] = dict(self.http.stats)
        return out

    def self_test(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(self.providers)))) \
                as pool:
            futures = {
                pool.submit(provider.self_test): pid
                for pid, provider in self.providers.items()
            }
            for future in as_completed(futures):
                pid = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append({"provider": pid, "ok": False,
                                    "message": str(exc)})
        for pid, message in self.setup_errors.items():
            results.append({"provider": pid, "ok": False, "message": message,
                            "skipped": True})
        results.sort(key=lambda item: item["provider"])
        return results

    # -- single lookup ---------------------------------------------------- #

    def lookup(self, mpn: str, manufacturer: str = "") -> LookupResult:
        result = LookupResult()
        term = clean(mpn)
        if not term:
            return result
        providers = self.active()
        if not providers:
            result.errors["*"] = "No data provider is available."
            return result

        if len(providers) == 1:
            self._lookup_one(providers[0], term, manufacturer, result)
        else:
            with ThreadPoolExecutor(max_workers=len(providers)) as pool:
                futures = [
                    pool.submit(self._lookup_one, provider, term,
                                manufacturer, result)
                    for provider in providers
                ]
                for future in futures:
                    future.result()

        ordered = [
            result.per_provider[pid] for pid in PROVIDER_PRIORITY
            if pid in result.per_provider
        ]
        ordered += [part for pid, part in result.per_provider.items()
                    if pid not in PROVIDER_PRIORITY]
        result.part = merge_parts(ordered)
        return result

    def _lookup_one(self, provider: Provider, mpn: str, manufacturer: str,
                    result: LookupResult) -> None:
        try:
            part = provider.lookup(mpn, manufacturer)
        except ProviderError as exc:
            with self._lock:
                result.errors[provider.id] = str(exc)
                result.providers_tried.append(provider.id)
            return
        except Exception as exc:  # pragma: no cover - defensive
            with self._lock:
                result.errors[provider.id] = f"Unexpected error: {exc}"
                result.providers_tried.append(provider.id)
            return
        with self._lock:
            result.providers_tried.append(provider.id)
            if part is not None:
                result.per_provider[provider.id] = part

    # -- batch ------------------------------------------------------------ #

    def lookup_many(self, requests: Sequence[tuple[str, str]],
                    max_workers: int | None = None,
                    progress: Callable[[int, int, str], None] | None = None,
                    should_cancel: Callable[[], bool] | None = None
                    ) -> dict[str, LookupResult]:
        """Look up many parts, returning ``{normalised_mpn: LookupResult}``.

        Deduplicates by ``(normalised mpn, manufacturer key)`` so a BOM with
        200 lines but 60 unique parts makes 60 lookups. Uses a provider's batch
        endpoint where available before fanning the remainder out over threads.
        """
        unique: dict[str, tuple[str, str]] = {}
        for mpn, manufacturer in requests:
            term = clean(mpn)
            if not term:
                continue
            key = normalize_mpn(term)
            if not key:
                continue
            existing = unique.get(key)
            # Prefer the variant that carries a manufacturer.
            if existing is None or (not clean(existing[1])
                                    and clean(manufacturer)):
                unique[key] = (term, clean(manufacturer))

        results: dict[str, LookupResult] = {key: LookupResult()
                                            for key in unique}
        if not unique:
            return results

        total = len(unique)
        done = 0
        providers = self.active()
        batch_capable = [p for p in providers if hasattr(p, "fetch_many")]

        # 1) batch providers first
        for provider in batch_capable:
            if should_cancel and should_cancel():
                break
            items = list(unique.items())
            chunk_size = 20
            for start in range(0, len(items), chunk_size):
                if should_cancel and should_cancel():
                    break
                chunk = items[start:start + chunk_size]
                try:
                    found = provider.fetch_many(  # type: ignore[attr-defined]
                        [value for _, value in chunk])
                except ProviderError as exc:
                    for key, _ in chunk:
                        results[key].errors[provider.id] = str(exc)
                    if not provider.available:
                        break
                    continue
                except Exception as exc:  # pragma: no cover
                    for key, _ in chunk:
                        results[key].errors[provider.id] = \
                            f"Unexpected error: {exc}"
                    continue
                for key, _ in chunk:
                    results[key].providers_tried.append(provider.id)
                    part = found.get(key)
                    if part is not None:
                        results[key].per_provider[provider.id] = part
                        if provider.part_cache is not None:
                            provider.part_cache.set(
                                provider.id, key, part.to_dict(),
                                manufacturer_key(part.manufacturer),
                                ttl=self.config.settings.part_cache_ttl)

        # 2) everything else, one thread per (part, provider) pair
        remaining = [p for p in providers if p not in batch_capable]
        if remaining:
            workers = max(1, min(max_workers or self.config.settings.max_workers,
                                 32))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {}
                for key, (mpn, manufacturer) in unique.items():
                    for provider in remaining:
                        future = pool.submit(self._lookup_one, provider, mpn,
                                             manufacturer, results[key])
                        futures[future] = key
                for future in as_completed(futures):
                    if should_cancel and should_cancel():
                        pool.shutdown(wait=False, cancel_futures=True)
                        break
                    try:
                        future.result()
                    except Exception as exc:  # pragma: no cover
                        LOG.debug("Lookup task failed: %s", exc)

        # 3) merge
        for key, result in results.items():
            ordered = [
                result.per_provider[pid] for pid in PROVIDER_PRIORITY
                if pid in result.per_provider
            ]
            ordered += [part for pid, part in result.per_provider.items()
                        if pid not in PROVIDER_PRIORITY]
            result.part = merge_parts(ordered)
            done += 1
            if progress:
                try:
                    progress(done, total, unique[key][0])
                except Exception:  # pragma: no cover
                    pass
        return results

    # -- search / alternates ---------------------------------------------- #

    def search(self, query: str, limit: int = 10) -> list[PartData]:
        """Keyword search across providers, merged by part number."""
        found: dict[str, list[PartData]] = {}
        providers = [p for p in self.active()]
        if not providers:
            return []
        with ThreadPoolExecutor(max_workers=len(providers)) as pool:
            futures = {pool.submit(_safe_search, provider, query, limit):
                       provider.id for provider in providers}
            for future in as_completed(futures):
                for part in future.result():
                    key = normalize_mpn(part.mpn)
                    if key:
                        found.setdefault(key, []).append(part)
        merged = [merge_parts(parts) for parts in found.values()]
        out = [part for part in merged if part is not None]
        out.sort(key=lambda part: (-(part.total_avail or 0), part.mpn))
        return out[:limit]

    def alternates(self, part: PartData, limit: int = 8) -> list[PartData]:
        """Provider-suggested substitutes, merged and de-duplicated."""
        providers = [p for p in self.active() if p.can_suggest_alternates]
        if not providers:
            return []
        found: dict[str, list[PartData]] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(providers))) as pool:
            futures = [pool.submit(_safe_alternates, provider, part, limit)
                       for provider in providers]
            for future in as_completed(futures):
                for candidate in future.result():
                    key = normalize_mpn(candidate.mpn)
                    if key and key != normalize_mpn(part.mpn):
                        found.setdefault(key, []).append(candidate)
        merged = [merge_parts(parts) for parts in found.values()]
        return [item for item in merged if item is not None][:limit]

    def close(self) -> None:
        pass  # HttpClient holds no persistent sockets worth closing


def _safe_search(provider: Provider, query: str, limit: int) -> list[PartData]:
    try:
        return provider.search(query, limit=limit)
    except Exception as exc:
        LOG.info("%s search failed: %s", provider.id, exc)
        return []


def _safe_alternates(provider: Provider, part: PartData, limit: int
                     ) -> list[PartData]:
    try:
        return provider.alternates(part, limit=limit)
    except Exception as exc:
        LOG.info("%s alternates failed: %s", provider.id, exc)
        return []


def _host_for(provider_id: str) -> str:
    return {
        "digikey": "api.digikey.com",
        "mouser": "api.mouser.com",
        "nexar": "api.nexar.com",
        "arrow": "api.arrow.com",
        "farnell": "api.element14.com",
        "lcsc": "wmsc.lcsc.com",
        "trustedparts": "api.trustedparts.com",
    }.get(provider_id, provider_id)


def describe_providers(config: Config) -> list[dict[str, Any]]:
    return config.describe_providers()


def available_provider_ids() -> Iterable[str]:
    return PROVIDER_CLASSES.keys()
