"""Mouser Search API v2 adapter (live when MOUSER_API_KEY is set)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import config_section, get_settings
from app.models import LifecycleStatus, SourceType
from app.sources.base import (
    Offer,
    PartFacts,
    PriceBreak,
    SourceAdapter,
    SourceResult,
    TokenBucket,
    parse_int,
)

_BASE = "https://api.mouser.com"
_STATUS_MAP = {
    "active": LifecycleStatus.ACTIVE.value,
    "prototype": LifecycleStatus.ACTIVE.value,
    "nrnd": LifecycleStatus.NRND.value,
    "not recommended": LifecycleStatus.NRND.value,
    "last time buy": LifecycleStatus.LAST_TIME_BUY.value,
    "obsolete": LifecycleStatus.OBSOLETE.value,
    "discontinued": LifecycleStatus.OBSOLETE.value,
    "eol": LifecycleStatus.OBSOLETE.value,
}


class MouserAdapter(SourceAdapter):
    name = "Mouser"
    source_type = SourceType.DISTRIBUTOR

    def __init__(self) -> None:
        _bucket_cfg = config_section("rate_limits")
        self._bucket = TokenBucket(float(_bucket_cfg.get("mouser_per_minute", 25)))

    @property
    def available(self) -> bool:
        return bool(get_settings().mouser_api_key)

    async def search(self, mpn: str, manufacturer: str, region: str, currency: str) -> SourceResult:
        key = get_settings().mouser_api_key
        if not key:
            return SourceResult(
                source_name=self.name,
                source_type=self.source_type,
                source_url=_BASE,
                retrieved_at=datetime.now(UTC),
                region=region,
                currency=currency,
                confidence=0.0,
                errors=["MOUSER_API_KEY not configured — using demo data instead."],
            )
        await self._bucket.acquire()
        if manufacturer.strip():
            url = f"{_BASE}/api/v2/search/partnumberandmanufacturer?apiKey={key}"
            payload = {
                "SearchByPartMfrNameRequest": {
                    "mouserPartNumber": mpn,
                    "manufacturerName": manufacturer,
                    "partSearchOptions": "Exact",
                }
            }
        else:
            url = f"{_BASE}/api/v2/search/partnumber?apiKey={key}"
            payload = {
                "SearchByPartRequest": {
                    "mouserPartNumber": mpn,
                    "partSearchOptions": "Exact",
                }
            }
        async with httpx.AsyncClient(timeout=config_section("orchestrator").get("timeout_seconds", 25)) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Accept": "application/json"},
            )
        if resp.status_code >= 400:
            return SourceResult(
                source_name=self.name,
                source_type=self.source_type,
                source_url=str(resp.url),
                retrieved_at=datetime.now(UTC),
                region=region,
                currency=currency,
                confidence=0.0,
                errors=[f"Mouser HTTP {resp.status_code}: {resp.text[:300]}"],
            )
        body = resp.json()
        search = (body or {}).get("SearchResults") or (body or {}).get("SearchResult") or {}
        parts = search.get("Parts") or []
        if not parts:
            errors = []
            for err in (body or {}).get("Errors") or []:
                errors.append(err.get("Message") or str(err))
            return SourceResult(
                source_name=self.name,
                source_type=self.source_type,
                source_url=str(resp.url),
                retrieved_at=datetime.now(UTC),
                region=region,
                currency=currency,
                confidence=0.0,
                errors=errors or [f"Mouser: no results for {mpn}."],
            )
        first = parts[0]
        return self._to_result(first, str(resp.url), region, currency)

    def _to_result(self, part: dict[str, Any], url: str, region: str, currency: str) -> SourceResult:
        status_raw = (part.get("LifecycleStatus") or "").strip().lower()
        status = LifecycleStatus.UNKNOWN.value
        for key, mapped in _STATUS_MAP.items():
            if key in status_raw:
                status = mapped
                break
        attrs = {
            (a.get("AttributeName") or "").strip().lower(): (a.get("AttributeValue") or "").strip()
            for a in part.get("ProductAttributes") or []
            if a.get("AttributeName") and a.get("AttributeValue")
        }
        compliance = part.get("Compliance") or {}
        rohs_raw = _pick(part, "ROHSStatus") or _pick(compliance, "RoHSStatus") or ""
        reach_raw = _pick(compliance, "REACHStatus") or attrs.get("reach status", "") or ""
        facts = PartFacts(
            manufacturer=part.get("Manufacturer") or "",
            manufacturer_mpn=part.get("ManufacturerPartNumber") or "",
            description=part.get("Description") or "",
            category=part.get("Category") or "",
            family=part.get("Series") or "",
            package=_pick(part, "PackageType", "Package/Case")
            or _attr(attrs, "package", "package / case", "case", "supplier package"),
            lifecycle_status=status,
            datasheet_url=_pick(part, "DataSheetUrl", "DatasheetUrl"),
            product_url=part.get("ProductDetailUrl") or "",
            rohs=rohs_raw or "Unknown",
            reach=reach_raw or "Unknown",
            operating_voltage=_attr(
                attrs,
                "operating voltage",
                "voltage - supply",
                "supply voltage",
                "output voltage",
                "vin (min)",
                "input voltage",
            ),
            current=_attr(
                attrs,
                "output current",
                "current - output",
                "supply current",
                "current - supply",
                "output current (max)",
            ),
            frequency=_attr(attrs, "frequency", "operating frequency", "clock frequency"),
            temperature=_attr(attrs, "operating temperature", "temperature range"),
        )
        breaks = []
        for b in part.get("PriceBreaks") or []:
            qty = parse_int(b.get("Quantity"))
            price = _to_float(b.get("Price"))
            if qty is not None and price is not None:
                breaks.append(PriceBreak(qty=qty, price=price, currency=currency))
        stock_raw = part.get("AvailabilityInStock") or part.get("Availability") or 0
        offer = Offer(
            distributor=self.name,
            available=bool(_to_float(stock_raw)),
            available_qty=parse_int(stock_raw),
            price_breaks=sorted(breaks, key=lambda b: b.qty),
            moq=parse_int(part.get("MinOrderQty")),
            lead_time=part.get("LeadTime") or "",
            currency=currency,
            region=region,
            url=part.get("ProductDetailUrl") or "",
        )
        return SourceResult(
            source_name=self.name,
            source_type=self.source_type,
            source_url=url,
            retrieved_at=datetime.now(UTC),
            region=region,
            currency=currency,
            confidence=0.92,
            facts=facts,
            offers=[offer] if breaks or offer.available_qty else [],
        )


def _pick(d: dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if v:
            return str(v)
    return ""


def _attr(attrs: dict[str, str], *keys: str) -> str:
    for k in keys:
        v = attrs.get(k)
        if v:
            return v
    return ""


def _to_float(value: Any) -> float | None:
    """Mouser returns prices/stock as strings like '$0.10' or '1,234' — coerce safely."""
    if value is None:
        return None
    try:
        cleaned = str(value).replace(",", "").replace("$", "").replace("\u00a0", "").strip()
        return float(cleaned)
    except (TypeError, ValueError):
        return None
