from datetime import UTC, datetime

from app.models import SourceType
from app.sources.base import Offer, PartFacts, PriceBreak, SourceResult
from app.validate import convert_price, merge_results


def _sr(name, stype, facts=None, offers=None, conf=0.9, currency="USD"):
    return SourceResult(
        source_name=name,
        source_type=stype,
        source_url=f"https://{name}.example",
        retrieved_at=datetime.now(UTC),
        region="US",
        currency=currency,
        confidence=conf,
        facts=facts,
        offers=offers or [],
    )


def test_price_conversion():
    assert convert_price(10, "EUR", "USD") > 10  # EUR worth more
    assert convert_price(10, "USD", "USD") == 10


def test_manufacturer_beats_distributor_on_lifecycle():
    mfr_facts = PartFacts(lifecycle_status="NRND", manufacturer="STMicroelectronics")
    mfr = _sr("ST", SourceType.MANUFACTURER, mfr_facts)
    dist = _sr(
        "Mouser", SourceType.DISTRIBUTOR, PartFacts(lifecycle_status="ACTIVE", manufacturer="STMicroelectronics")
    )
    m = merge_results([mfr, dist])
    assert m.facts.lifecycle_status == "NRND"
    assert m.facts.manufacturer == "STMicroelectronics"


def test_conflict_is_reported():
    mfr = _sr("ST", SourceType.MANUFACTURER, PartFacts(lifecycle_status="ACTIVE"))
    dist = _sr("Mouser", SourceType.DISTRIBUTOR, PartFacts(lifecycle_status="OBSOLETE"))
    m = merge_results([mfr, dist])
    assert m.facts.lifecycle_status == "ACTIVE"
    assert any("OBSOLETE" in c for c in m.conflicts) or any(e.status == "conflicting" for e in m.evidence)


def test_mock_lowers_confidence():
    live = _sr("Mouser", SourceType.DISTRIBUTOR, PartFacts(description="X"), conf=0.9)
    mock = _sr("Mouser (demo)", SourceType.MOCK, PartFacts(description="X"), conf=0.6)
    m = merge_results([live, mock])
    assert m.used_mock is True
    assert m.confidence < 0.85


def test_offers_currency_normalized():
    offer = Offer(
        distributor="TME",
        available=True,
        available_qty=5,
        price_breaks=[PriceBreak(1, 10.0, "EUR"), PriceBreak(10, 9.0, "EUR")],
        currency="EUR",
        region="EU",
    )
    sr = _sr("TME", SourceType.DISTRIBUTOR, offers=[offer], currency="EUR")
    m = merge_results([sr])
    assert len(m.offers) == 1
    assert all(b.currency == "USD" for b in m.offers[0].price_breaks)
    assert m.offers[0].price_breaks[0].price > 10  # EUR 10 in USD


def test_unknown_lifecycle_does_not_override():
    dist = _sr("Mouser", SourceType.DISTRIBUTOR, PartFacts(lifecycle_status="ACTIVE"))
    m = merge_results([dist])
    assert m.facts.lifecycle_status == "ACTIVE"
