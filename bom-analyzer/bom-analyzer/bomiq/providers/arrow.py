"""
Arrow Electronics provider -- ItemService v4.

Endpoint
--------
``GET https://api.arrow.com/itemservice/v4/en/search/token``
with ``api_key``, ``login`` and ``search_token`` parameters.

Arrow's schema has shifted between revisions, so the parser here walks the
response defensively: it looks for the well-known containers and falls back to
a recursive scan for an object that carries a manufacturer part number. That
keeps the adapter working when Arrow renames a wrapper key.

Disabled by default; enable it in Settings once you have portal credentials.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..core.models import PartData
from ..util import log
from ..util.money import normalize_currency
from ..util.text import clean, normalize_mpn
from ..util.units import parse_int
from .base import (
    Provider, build_price_breaks, normalize_compliance_state,
    normalize_lifecycle,
)

LOG = log.get("providers.arrow")

BASE = "https://api.arrow.com/itemservice/v4/en"


class ArrowProvider(Provider):
    id = "arrow"
    default_currency = "USD"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.currency = normalize_currency(self.config.settings.currency, "USD")

    def fetch(self, mpn: str, manufacturer: str = "") -> PartData | None:
        response = self.http.get(
            f"{BASE}/search/token",
            params={
                "api_key": self.credential("api_key"),
                "login": self.credential("login", required=False) or "",
                "search_token": clean(mpn),
                "resources": "all",
            },
            headers={"Accept": "application/json"},
            cache_ttl=self.config.settings.http_cache_ttl,
            allow_status=(404,),
        )
        if response.status == 404:
            return None
        payload = response.json() or {}
        items = list(_iter_items(payload))
        if not items:
            return None
        target = normalize_mpn(mpn)

        def score(item: dict[str, Any]) -> tuple[int, int]:
            candidate = normalize_mpn(
                item.get("partNum") or item.get("itemId") or
                item.get("manufacturerPartNumber"))
            exact = 2 if candidate == target else (
                1 if target and target in candidate else 0)
            return exact, _total_stock(item)

        best = max(items, key=score)
        return self._to_part(best)

    # -- mapping ---------------------------------------------------------- #

    def _to_part(self, item: dict[str, Any]) -> PartData:
        mpn = clean(item.get("partNum") or item.get("itemId")
                    or item.get("manufacturerPartNumber"))
        manufacturer = clean(item.get("manufacturer")) or clean(
            (item.get("manufacturerInfo") or {}).get("manufacturerName"))
        status_fields = [
            item.get("status"), item.get("lifeCycle"),
            item.get("lifecycleStatus"), item.get("productStatus"),
            (item.get("itemStatus") or {}).get("status")
            if isinstance(item.get("itemStatus"), dict) else None,
        ]
        lifecycle, note = normalize_lifecycle(*status_fields)

        part = PartData(
            mpn=mpn, manufacturer=manufacturer,
            description=clean(item.get("description")
                              or item.get("desc")
                              or item.get("shortDescription")),
            category=clean(_first_text(item.get("taxonomy"))),
            lifecycle=lifecycle, lifecycle_note=clean(note),
            datasheet_url=clean(_first_url(item.get("resources"), "datasheet")),
            image_url=clean(_first_url(item.get("resources"), "image")),
            product_url=clean(_first_url(item.get("resources"), "cloud_part")
                              or item.get("webSiteUrl")),
            providers=[self.id],
        )
        for spec in item.get("specifications") or []:
            if isinstance(spec, dict):
                name = clean(spec.get("name") or spec.get("attributeName"))
                value = clean(spec.get("value") or spec.get("attributeValue"))
                if name and value:
                    part.specs[name] = value
        part.package = part.specs.get("Package", "") or \
            part.specs.get("Package / Case", "")

        compliance = self.compliance_from_specs(part.specs)
        rohs = clean(item.get("rohs") or item.get("rohsStatus"))
        if rohs:
            compliance.rohs = normalize_compliance_state(rohs)
            compliance.rohs_note = rohs
        compliance.country_of_origin = compliance.country_of_origin or clean(
            item.get("countryOfOrigin"))
        compliance.hts_code = compliance.hts_code or clean(
            item.get("htsCode") or item.get("hts"))
        compliance.eccn = compliance.eccn or clean(item.get("eccn"))
        compliance.sources.append("Arrow")
        part.compliance = compliance

        for source in _iter_sources(item):
            offer = self._source_to_offer(source, part)
            if offer is not None:
                part.offers.append(offer)
        part.total_avail = part.total_stock or None
        return part

    def _source_to_offer(self, source: dict[str, Any], part: PartData) -> Any:
        availability = source.get("availability") or []
        if isinstance(availability, dict):
            availability = [availability]
        stock = 0
        lead_days = None
        for entry in availability:
            if not isinstance(entry, dict):
                continue
            stock += parse_int(entry.get("fohQuantity")
                               or entry.get("availableQuantity"), 0) or 0
            lead_days = lead_days or self.lead_days(
                entry.get("leadTime"), entry.get("factoryLeadTime"))

        prices_block = source.get("pricing") or source.get("prices") or {}
        ladder = []
        if isinstance(prices_block, dict):
            ladder = prices_block.get("resaleList") or \
                prices_block.get("priceBreaks") or []
        elif isinstance(prices_block, list):
            ladder = prices_block
        currency = normalize_currency(
            (prices_block or {}).get("currencyCode")
            if isinstance(prices_block, dict) else self.currency,
            self.currency)
        breaks = build_price_breaks(
            ladder, currency,
            qty_keys=("minQty", "MinQty", "quantity", "breakQuantity"),
            price_keys=("resalePrice", "price", "unitPrice"))

        if not breaks and not stock:
            return None
        return self.make_offer(
            distributor="Arrow Electronics",
            sku=clean(source.get("sourcePartId") or source.get("partId")),
            packaging=clean(source.get("packaging")),
            stock=stock or None,
            moq=parse_int(source.get("minOrderQuantity")
                          or source.get("minimumOrderQuantity"), 1),
            spq=parse_int(source.get("packQuantity")
                          or source.get("orderMultiple")),
            lead_time_days=0 if stock else lead_days,
            price_breaks=breaks,
            currency=currency,
            url=part.product_url,
            region=clean(source.get("region")) or "global",
            authorized=True,
        )

    def test_mpn(self) -> str:
        return "LM358DR"


# --------------------------------------------------------------------------- #
# Tolerant response walking
# --------------------------------------------------------------------------- #

_ITEM_MARKERS = ("partNum", "manufacturerPartNumber", "itemId")


def _iter_items(payload: Any, depth: int = 0) -> Iterator[dict[str, Any]]:
    """Yield every object in the payload that looks like a catalogue item."""
    if depth > 8:
        return
    if isinstance(payload, dict):
        if any(clean(payload.get(marker)) for marker in _ITEM_MARKERS) and (
                "description" in payload or "sources" in payload
                or "specifications" in payload or "manufacturer" in payload):
            yield payload
            return
        for value in payload.values():
            yield from _iter_items(value, depth + 1)
    elif isinstance(payload, list):
        for value in payload:
            yield from _iter_items(value, depth + 1)


def _iter_sources(item: dict[str, Any]) -> Iterator[dict[str, Any]]:
    sources = item.get("sources") or item.get("sourceParts") or []
    if isinstance(sources, dict):
        sources = [sources]
    for source in sources:
        if isinstance(source, dict):
            nested = source.get("sourceParts")
            if isinstance(nested, list) and nested:
                for entry in nested:
                    if isinstance(entry, dict):
                        yield entry
            else:
                yield source


def _total_stock(item: dict[str, Any]) -> int:
    total = 0
    for source in _iter_sources(item):
        availability = source.get("availability") or []
        if isinstance(availability, dict):
            availability = [availability]
        for entry in availability:
            if isinstance(entry, dict):
                total += parse_int(entry.get("fohQuantity")
                                   or entry.get("availableQuantity"), 0) or 0
    return total


def _first_text(value: Any) -> str:
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict):
            for key in ("title", "name", "description", "class"):
                if clean(first.get(key)):
                    return clean(first[key])
        return clean(first)
    if isinstance(value, dict):
        for key in ("title", "name", "description"):
            if clean(value.get(key)):
                return clean(value[key])
    return clean(value)


def _first_url(resources: Any, kind: str) -> str:
    if not isinstance(resources, list):
        return ""
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        resource_type = clean(resource.get("type")).lower()
        if kind in resource_type:
            return clean(resource.get("uri") or resource.get("url"))
    return ""
