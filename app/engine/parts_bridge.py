"""Bridge between the legacy /parts pipeline and the BOM-IQ engine.

Replaces the pre-merge ``app.orchestrator`` + ``app.sources`` adapters. All
lookups are delegated to :class:`bomiq.engine.Engine.registry` and converted
back into the legacy :class:`app.sources.base.SourceResult` shapes consumed by
:mod:`app.service`, :mod:`app.validate`, :mod:`app.risk` and the old UI.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from app.config import currency, region
from app.engine import bridge
from app.engine.bomiq.config import PROVIDER_SPECS
from app.engine.bomiq.core.models import ComplianceState, Lifecycle, PartData
from app.engine.bomiq.providers.mock import SEED, MockProvider
from app.models import LifecycleEventType, SourceType
from app.sources.base import (
    LifecycleNotice,
    Offer,
    PartFacts,
    PriceBreak,
    SourceResult,
)

logger = logging.getLogger("schemata_bom.parts")

_CONFIDENCE_LIVE = 0.9
_CONFIDENCE_MOCK = 0.55

_LIFECYCLE_MAP: dict[str, str] = {
    Lifecycle.NEW.value: "ACTIVE",
    Lifecycle.ACTIVE.value: "ACTIVE",
    Lifecycle.NRND.value: "NRND",
    Lifecycle.EOL.value: "EOL_ANNOUNCED",
    Lifecycle.OBSOLETE.value: "OBSOLETE",
    Lifecycle.UNKNOWN.value: "UNKNOWN",
}

_LIFECYCLE_TITLE: dict[str, str] = {
    "ACTIVE": "Active",
    "NRND": "Not Recommended for New Design (NRND)",
    "EOL_ANNOUNCED": "EOL Announced",
    "OBSOLETE": "Obsolete",
    "UNKNOWN": "Unknown",
}

_ROHS_TEXT: dict[str, str] = {
    ComplianceState.COMPLIANT.value: "RoHS3 Compliant",
    ComplianceState.NON_COMPLIANT.value: "Not RoHS compliant",
    ComplianceState.EXEMPT.value: "RoHS compliant by exemption",
    ComplianceState.UNKNOWN.value: "Unknown",
}

_REACH_TEXT: dict[str, str] = {
    ComplianceState.COMPLIANT.value: "REACH compliant",
    ComplianceState.NON_COMPLIANT.value: "REACH non-compliant",
    ComplianceState.EXEMPT.value: "Unknown",
    ComplianceState.UNKNOWN.value: "Unknown",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _lifecycle_value(part: PartData) -> str:
    value = part.lifecycle if isinstance(part.lifecycle, Lifecycle) else \
        Lifecycle.UNKNOWN
    return value.value  # "New" | "Active" | "NRND" | "EOL" | "Obsolete" | "Unknown"


def _lifecycle_status(part: PartData) -> str:
    return _LIFECYCLE_MAP.get(_lifecycle_value(part), "unknown")


def _lifecycle_title(part: PartData) -> str:
    return _LIFECYCLE_TITLE.get(_lifecycle_status(part), "Unknown")


def _notices(part: PartData, source_name: str) -> list[LifecycleNotice]:
    event_type = {
        Lifecycle.EOL.value: LifecycleEventType.EOL_ANNOUNCED.value,
        Lifecycle.OBSOLETE.value: LifecycleEventType.EOL_ANNOUNCED.value,
    }.get(_lifecycle_value(part))
    if event_type is None:
        return []
    return [
        LifecycleNotice(
            event_type=event_type,
            notes=f"{source_name} reports lifecycle status "
                  f"{_lifecycle_title(part)}.",
            source_name=source_name,
            source_url=part.product_url or "",
        )
    ]


def _compliance_text(compliance: Any, mapping: dict[str, str],
                     note_field: str) -> str:
    try:
        note = getattr(compliance, note_field, "")
    except Exception:  # noqa: BLE001 - defensive against partial compliance
        note = ""
    if note and str(note).strip():
        return str(note).strip()
    state = getattr(compliance, "rohs", ComplianceState.UNKNOWN) if \
        note_field == "rohs_note" else \
        getattr(compliance, "reach", ComplianceState.UNKNOWN)
    value = state.value if isinstance(state, ComplianceState) else \
        str(state) if state else ComplianceState.UNKNOWN.value
    return mapping.get(value, mapping.get(ComplianceState.UNKNOWN.value))


def _rohs(part: PartData) -> str:
    return _compliance_text(part.compliance, _ROHS_TEXT, "rohs_note")


def _reach(part: PartData) -> str:
    return _compliance_text(part.compliance, _REACH_TEXT, "reach_note")


_VOLTAGE_KEYS = ("Operating Voltage", "Voltage", "Supply Voltage",
                 "Voltage - Supply (Vcc/Vdd)")
_CURRENT_KEYS = ("Current", "Operating Current", "Current - Output")
_FREQUENCY_KEYS = ("Frequency", "Frequency - Max", "Max Frequency")
_TEMPERATURE_KEYS = ("Operating Temperature", "Temperature",
                     "Temperature Range")


def _spec(part: PartData, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = part.specs.get(key)
        if value is not None:
            return str(value)
    return ""


def _min_unit_price(part: PartData) -> float | None:
    prices: list[float] = []
    for offer in part.offers:
        unit = offer.min_unit_price
        parsed = _as_float(unit)
        if parsed is not None and parsed > 0:
            prices.append(parsed)
    return min(prices) if prices else None


def part_to_candidate(part: PartData) -> dict[str, Any]:
    """Convert an engine part into a legacy catalogue candidate dict."""
    available_qty = sum(int(o.stock or 0) for o in part.offers)
    return {
        "mpn": part.mpn,
        "manufacturer": part.manufacturer or "",
        "category": part.category or "",
        "description": part.description or "",
        "package": part.package or "",
        "lifecycle_status": _lifecycle_status(part),
        "operating_voltage": _spec(part, _VOLTAGE_KEYS),
        "current": _spec(part, _CURRENT_KEYS),
        "frequency": _spec(part, _FREQUENCY_KEYS),
        "temperature": _spec(part, _TEMPERATURE_KEYS),
        "available": available_qty > 0,
        "available_qty": available_qty,
        "min_price": _min_unit_price(part),
    }


@lru_cache(maxsize=1)
def _catalogue() -> tuple[PartData, ...]:
    provider = MockProvider(bridge.get_engine().config)
    parts: list[PartData] = []
    for key in sorted(SEED):
        part = provider.fetch(key)
        if part is not None:
            parts.append(part)
    return tuple(parts)


def catalogue_parts() -> list[PartData]:
    """Deterministic demo catalogue derived from the engine's mock provider."""
    return list(_catalogue())


def source_status() -> dict[str, Any]:
    """Legacy ``{"live", "mock", "configured"}`` status from the engine."""
    engine = bridge.get_engine()
    live: list[str] = []
    mock: list[str] = []
    configured: dict[str, bool] = {}
    for row in engine.config.describe_providers():
        provider_id = str(row.get("id", ""))
        configured[provider_id] = bool(row.get("configured", False))
        if not row.get("active", False):
            continue
        name = str(row.get("name") or provider_id)
        if row.get("kind") == "offline":
            mock.append(name)
        else:
            live.append(name)
    return {"live": live, "mock": mock, "configured": configured}


def _to_offer(offer: Any) -> Offer:
    breaks = [
        PriceBreak(
            qty=int(break_.quantity),
            price=_as_float(break_.unit_price, 0.0) or 0.0,
            currency=str(break_.currency or "USD"),
        )
        for break_ in offer.real_price_breaks
    ]
    in_stock = bool(offer.in_stock)
    if in_stock:
        lead_time = "In stock"
    elif offer.lead_time_days:
        lead_time = f"{max(1, round(int(offer.lead_time_days) / 7))} weeks"
    else:
        lead_time = ""
    return Offer(
        distributor=str(offer.distributor or "Distributor"),
        available=in_stock,
        available_qty=int(offer.stock or 0),
        price_breaks=breaks,
        moq=offer.moq,
        lead_time=lead_time,
        currency=str(offer.currency or "USD"),
        region=str(offer.region or region()),
        url=str(offer.url or ""),
    )


def part_to_source_result(provider_id: str, part: PartData) -> SourceResult:
    """Convert one provider's :class:`PartData` to a legacy :class:`SourceResult`."""
    spec = PROVIDER_SPECS.get(provider_id)
    source_name = spec.name if spec else provider_id.title()
    is_mock = provider_id == "mock"
    return SourceResult(
        source_name=source_name,
        source_type=SourceType.MOCK if is_mock else SourceType.DISTRIBUTOR,
        source_url=part.product_url or part.datasheet_url or "",
        retrieved_at=_utcnow(),
        region=region(),
        currency=currency(),
        confidence=_CONFIDENCE_MOCK if is_mock else _CONFIDENCE_LIVE,
        facts=PartFacts(
            manufacturer=part.manufacturer or "",
            manufacturer_mpn=part.mpn,
            description=part.description or "",
            category=part.category or "",
            family="",
            package=part.package or "",
            lifecycle_status=_lifecycle_status(part),
            datasheet_url=part.datasheet_url or "",
            product_url=part.product_url or "",
            rohs=_rohs(part),
            reach=_reach(part),
            operating_voltage=_spec(part, _VOLTAGE_KEYS),
            current=_spec(part, _CURRENT_KEYS),
            frequency=_spec(part, _FREQUENCY_KEYS),
            temperature=_spec(part, _TEMPERATURE_KEYS),
        ),
        offers=[_to_offer(offer) for offer in part.offers],
        lifecycle_notices=_notices(part, source_name),
    )


def _error_result(errors: list[str]) -> SourceResult:
    return SourceResult(
        source_name="Engine",
        source_type=SourceType.DISTRIBUTOR,
        source_url="",
        retrieved_at=_utcnow(),
        region=region(),
        currency=currency(),
        confidence=0.0,
        errors=errors or ["No source returned data."],
    )


def _is_synthetic(part: PartData) -> bool:
    """True when the engine mock synthesised a placeholder for an unknown part."""
    return str(part.specs.get("Record type", "")).startswith("Synthesised")


def _lookup_sync(mpn: str, manufacturer: str) -> list[SourceResult]:
    engine = bridge.get_engine()
    lookup = engine.registry().lookup(mpn, manufacturer)
    results = [
        part_to_source_result(provider_id, part)
        for provider_id, part in (lookup.per_provider or {}).items()
        if part is not None and not _is_synthetic(part)
    ]
    if results:
        return results
    errors = list((lookup.errors or {}).values()) or \
        [f"No source returned data for {mpn}."]
    return [_error_result(errors)]


async def lookup_part(mpn: str, manufacturer: str = "") -> list[SourceResult]:
    """Look up a part across every live engine provider (blocking in a pool)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        bridge._executor, _lookup_sync, mpn, manufacturer,
    )
