"""Demo/stub data sources backed by the fixture catalog.

Used automatically when no live API credential exists for a source, and
always for sources without a public free API (Farnell, RS, TME, LCSC,
manufacturer lifecycle/PCN). Results are tagged MOCK so confidence and
the UI can distinguish demo data from live-verified data.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.models import SourceType
from app.sources.base import (
    LifecycleNotice,
    Offer,
    PartFacts,
    PriceBreak,
    SourceAdapter,
    SourceResult,
)
from app.sources.demo_catalog import get

_DEMO_NOTE = "Synthetic demo data from the built-in catalog. Add an API key to go live."


class MfrStub(SourceAdapter):
    """Manufacturer datasheet/lifecycle/PCN source (demo)."""

    name = "Manufacturer (demo)"
    source_type = SourceType.MOCK

    async def search(self, mpn: str, manufacturer: str, region: str, currency: str) -> SourceResult:
        part = get(mpn)
        if part is None:
            return SourceResult(
                source_name=self.name,
                source_type=self.source_type,
                source_url="",
                retrieved_at=datetime.now(UTC),
                region=region,
                currency=currency,
                confidence=0.0,
                errors=[f"No demo catalog entry for {mpn}."],
            )
        facts = PartFacts(
            manufacturer=part.manufacturer,
            manufacturer_mpn=part.mpn,
            description=part.description,
            category=part.category,
            family=part.family,
            package=part.package,
            lifecycle_status=part.lifecycle,
            datasheet_url=part.datasheet,
            product_url=part.product_url,
            rohs=part.rohs,
            reach=part.reach,
            operating_voltage=part.voltage,
            current=part.current,
            frequency=part.frequency,
            temperature=part.temperature,
        )
        notices = [
            LifecycleNotice(
                event_type=e[0],
                event_date=date.fromisoformat(e[1]) if len(e) > 1 and e[1] else None,
                notes=e[2] if len(e) > 2 else "",
                source_name=self.name,
            )
            for e in part.notices
        ]
        return SourceResult(
            source_name=self.name,
            source_type=self.source_type,
            source_url=part.product_url,
            retrieved_at=datetime.now(UTC),
            region=region,
            currency=currency,
            confidence=0.6,
            facts=facts,
            lifecycle_notices=notices,
            doc_reference=f"demo://catalog/{part.mpn}",
            errors=[_DEMO_NOTE],
        )


class _DistStub(SourceAdapter):
    source_type = SourceType.MOCK

    def __init__(self, persona: str, match_names: tuple[str, ...]):
        self.persona = persona
        self.name = f"{persona} (demo)"
        self._match = match_names

    async def search(self, mpn: str, manufacturer: str, region: str, currency: str) -> SourceResult:
        part = get(mpn)
        if part is None:
            return SourceResult(
                source_name=self.name,
                source_type=SourceType.MOCK,
                source_url="",
                retrieved_at=datetime.now(UTC),
                region=region,
                currency=currency,
                confidence=0.0,
                errors=[f"No demo catalog entry for {mpn}."],
            )
        offers: list[Offer] = []
        for label in part.offers:
            if label in self._match or any(m in label.lower() for m in self._match):
                demo = part.offers[label]
                breaks = [PriceBreak(qty=b[0], price=b[1], currency=currency) for b in demo["breaks"]]
                offers.append(
                    Offer(
                        distributor=self.persona,
                        available=demo["stock"] > 0,
                        available_qty=demo["stock"],
                        price_breaks=breaks,
                        moq=breaks[0].qty if breaks else None,
                        lead_time=demo["lead"],
                        currency=currency,
                        region=region,
                        url=demo["url"],
                    )
                )
        return SourceResult(
            source_name=self.name,
            source_type=SourceType.MOCK,
            source_url=offers[0].url if offers else "",
            retrieved_at=datetime.now(UTC),
            region=region,
            currency=currency,
            confidence=0.6 if offers else 0.0,
            facts=None,
            offers=offers,
            errors=[_DEMO_NOTE],
        )


def _dist_stub(persona: str, matches: tuple[str, ...]) -> _DistStub:
    return _DistStub(persona, matches)


farnell_stub = _dist_stub("Farnell", ("Farnell",))
rs_stub = _dist_stub("RS", ("RS",))
tme_stub = _dist_stub("TME", ("TME", "LCSC"))
lcsc_stub = _dist_stub("LCSC", ("LCSC",))
mouser_stub = _dist_stub("Mouser", ("Mouser",))
digikey_stub = _dist_stub("DigiKey", ("DigiKey",))
manufacturer_stub = MfrStub()

STUB_ADAPTERS: list[SourceAdapter] = [
    manufacturer_stub,
    mouser_stub,
    digikey_stub,
    farnell_stub,
    rs_stub,
    tme_stub,
    lcsc_stub,
]
