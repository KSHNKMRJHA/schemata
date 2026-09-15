"""Source Orchestrator: dispatch to live + stub adapters with rate limits,
bounded concurrency, retries, and graceful degradation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.config import config_section, get_settings
from app.models import SourceType
from app.sources.base import SourceResult
from app.sources.digikey import DigiKeyAdapter
from app.sources.local import (
    digikey_stub,
    farnell_stub,
    lcsc_stub,
    manufacturer_stub,
    mouser_stub,
    nexar_stub,
    rs_stub,
    tme_stub,
)
from app.sources.mouser import MouserAdapter
from app.sources.nexar import NexarAdapter


class Orchestrator:
    """Builds the adapter set once; each source runs independently."""

    def __init__(self) -> None:
        self.adapters = self._build_adapters()

    def _build_adapters(self) -> list:
        mouser = MouserAdapter()
        digikey = DigiKeyAdapter()
        nexar = NexarAdapter()
        adapters = []
        adapters.append(mouser if mouser.available else mouser_stub)
        adapters.append(digikey if digikey.available else digikey_stub)
        adapters.append(nexar if nexar.available else nexar_stub)
        adapters += [manufacturer_stub, farnell_stub, rs_stub, tme_stub, lcsc_stub]
        return adapters

    def live_sources(self) -> list[str]:
        return [a.name for a in self.adapters if not a.source_type == SourceType.MOCK]

    def mock_sources(self) -> list[str]:
        return [a.name for a in self.adapters if a.source_type == SourceType.MOCK]

    async def collect(self, mpn: str, manufacturer: str, region: str, currency: str) -> list[SourceResult]:
        """Query every configured source concurrently and gather results."""
        cfg = config_section("orchestrator")
        semaphore = asyncio.Semaphore(int(cfg.get("concurrency", 4)))
        max_retries = int(cfg.get("max_retries", 2))
        base_delay = 0.4

        async def run(adapter) -> SourceResult:
            async with semaphore:
                for attempt in range(max_retries + 1):
                    try:
                        return await adapter.search(mpn, manufacturer, region, currency)
                    except Exception as exc:  # noqa: BLE001 - network adapters can throw anything
                        last_error = f"{adapter.name}: {exc}"
                        if attempt < max_retries:
                            await asyncio.sleep(base_delay * (2**attempt))
                return SourceResult(
                    source_name=getattr(adapter, "name", "source"),
                    source_type=getattr(adapter, "source_type", SourceType.DISTRIBUTOR),
                    source_url="",
                    retrieved_at=datetime.now(UTC),
                    region=region,
                    currency=currency,
                    confidence=0.0,
                    errors=[f"{last_error} (after retries)"],
                )

        results = await asyncio.gather(*(run(a) for a in self.adapters))
        return [r for r in results if r is not None]

    def configured(self) -> dict[str, bool]:
        s = get_settings()
        return {
            "mouser": bool(s.mouser_api_key),
            "digikey": bool(s.digikey_client_id and s.digikey_client_secret),
            "nexar": bool(s.nexar_client_id and s.nexar_client_secret),
        }


_orchestrator: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator


def reload_orchestrator() -> Orchestrator:
    """Rebuild the adapter set after credential changes (live vs stub switch)."""
    global _orchestrator
    _orchestrator = None
    return get_orchestrator()
