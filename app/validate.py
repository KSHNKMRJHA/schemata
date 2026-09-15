"""Normalize + Validate: merge raw source results into canonical component
facts, attached evidence with provenance + conflict status, currency/unit
normalization, and a source-precedence aware confidence estimate."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.config import currency as default_currency
from app.models import EvidenceStatus, LifecycleStatus, SourceType
from app.sources.base import Offer, PartFacts, SourceResult

_FACT_FIELDS = [
    "manufacturer",
    "manufacturer_mpn",
    "description",
    "category",
    "family",
    "package",
    "lifecycle_status",
    "datasheet_url",
    "product_url",
    "rohs",
    "reach",
    "operating_voltage",
    "current",
    "frequency",
    "temperature",
]

# USD baseline reference rates (static, tunable in config later)
_CURRENCY_TO_USD = {"USD": 1.0, "EUR": 1.09, "GBP": 1.27, "INR": 0.012, "JPY": 0.0067, "CNY": 0.14}


def convert_price(value: float, from_currency: str, to_currency: str) -> float:
    if not value:
        return 0.0
    usd = value * _CURRENCY_TO_USD.get(from_currency.upper(), 1.0)
    return round(usd / _CURRENCY_TO_USD.get(to_currency.upper(), 1.0), 4)


def _rank(sr: SourceResult) -> int:
    """Lower is more authoritative: mfr > distributor > mock-any."""
    if sr.source_type == SourceType.MANUFACTURER:
        return 0
    if sr.source_type == SourceType.DISTRIBUTOR:
        return 1
    return 2  # MOCK


@dataclass
class EvidenceEntry:
    source_name: str
    source_type: str
    source_url: str
    retrieved_at: datetime
    region: str
    currency: str
    normalized_value: dict
    confidence: float
    status: str
    doc_reference: str = ""


@dataclass
class MergedComponent:
    facts: PartFacts
    witnesses: dict[str, str]  # field -> provider source name
    evidence: list[EvidenceEntry] = field(default_factory=list)
    offers: list[Offer] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    confidence: float = 0.0
    used_mock: bool = False

    @property
    def provider(self) -> str:
        return self.witnesses.get("lifecycle_status", "")


def merge_results(results: list[SourceResult]) -> MergedComponent:
    """Fold source results into one canonical component record."""
    ranked = sorted((r for r in results if r.facts is not None or r.offers), key=_rank)

    winners: dict[str, tuple[str, SourceResult]] = {}
    for sr in ranked:
        if sr.facts is None:
            continue
        for fname in _FACT_FIELDS:
            value = getattr(sr.facts, fname)
            if value in ("", None) or fname == "lifecycle_status" and value == LifecycleStatus.UNKNOWN.value:
                continue
            if fname not in winners:
                winners[fname] = (value, sr)

    facts = PartFacts()
    witnesses: dict[str, str] = {}
    for fname in _FACT_FIELDS:
        value, provider = winners.get(fname, ("", None))
        if value and (fname != "lifecycle_status" or value != LifecycleStatus.UNKNOWN.value):
            setattr(facts, fname, value)
            if provider is not None:
                witnesses[fname] = provider.source_name

    conflicts: list[str] = []
    evidence: list[EvidenceEntry] = []
    all_results = sorted(results, key=_rank)
    for sr in all_results:
        if sr.facts is None and not sr.offers and not sr.errors:
            continue  # nothing claimed, nothing reported — no evidence to record
        claimed: dict[str, str] = {}
        contested = False
        if sr.facts is not None:
            for fname in _FACT_FIELDS:
                value = getattr(sr.facts, fname)
                if value in ("", None):
                    continue
                claimed[fname] = str(value)
                winner, _ = winners.get(fname, ("", None))
                if winner and str(value) != str(winner):
                    contested = True
                    conflict_rank = _rank(sr) >= _rank(winners[fname][1]) if winners[fname][1] else True
                    if conflict_rank:
                        conflicts.append(
                            f"{sr.source_name} reports {fname}={value!r} while {winners[fname][1].source_name} "
                            f"reports {winner!r}"
                        )
        status = EvidenceStatus.UNKNOWN.value
        if not claimed:
            status = EvidenceStatus.UNKNOWN.value  # source reported nothing usable
        elif sr.is_mock:
            status = EvidenceStatus.UNKNOWN.value if contested else EvidenceStatus.VERIFIED.value
        elif not contested and sr.confidence >= 0.5:
            status = EvidenceStatus.VERIFIED.value
        elif contested:
            status = EvidenceStatus.CONFLICTING.value
        evidence.append(
            EvidenceEntry(
                source_name=sr.source_name,
                source_type=sr.source_type.value,
                source_url=sr.source_url,
                retrieved_at=sr.retrieved_at,
                region=sr.region,
                currency=sr.currency,
                normalized_value=claimed,
                confidence=sr.confidence,
                status=status,
                doc_reference=sr.doc_reference,
            )
        )

    offers = _normalize_offers(results)
    used_mock = any(r.is_mock for r in results)
    confidence = _estimate_confidence(ranked, winners, used_mock)

    return MergedComponent(
        facts=facts,
        witnesses=witnesses,
        evidence=evidence,
        offers=offers,
        conflicts=conflicts,
        confidence=confidence,
        used_mock=used_mock,
    )


def _normalize_offers(results: list[SourceResult], target: str | None = None) -> list[Offer]:
    """Convert all price breaks to one currency and de-duplicate by distributor."""
    target = target or default_currency()
    merged: dict[str, Offer] = {}
    for sr in results:
        for offer in sr.offers:
            key = offer.distributor.lower()
            normalized = Offer(
                distributor=offer.distributor,
                available=offer.available,
                available_qty=offer.available_qty,
                price_breaks=[
                    type(offer.price_breaks[0])(
                        qty=b.qty,
                        price=convert_price(b.price, b.currency, target) if b.currency else b.price,
                        currency=target,
                    )
                    for b in offer.price_breaks
                ]
                if offer.price_breaks
                else [],
                moq=offer.moq,
                lead_time=offer.lead_time,
                currency=target,
                region=offer.region or sr.region,
                url=offer.url,
            )
            normalized.price_breaks = sorted(
                (b for b in normalized.price_breaks if b.qty and b.price),
                key=lambda b: b.qty,
            )
            prior = merged.get(key)
            if prior is None or (not prior.available and normalized.available):
                merged[key] = normalized
    return list(merged.values())


def _estimate_confidence(ranked: list[SourceResult], winners: dict, used_mock: bool) -> float:
    if not ranked:
        return 0.0
    total_weight = 0.0
    weighted = 0.0
    for sr in ranked:
        weight = 1.0 / (_rank(sr) + 1)
        total_weight += weight
        weighted += sr.confidence * weight
    base = weighted / total_weight if total_weight else 0.0
    if used_mock:
        base *= 0.85
    return round(max(0.0, min(1.0, base)), 3)
