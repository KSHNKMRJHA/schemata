"""
Mouser provider -- Search API v1.

Authentication is a single API key passed as the ``apiKey`` query parameter.

Endpoints used
--------------
``POST /api/v1/search/partnumber``  -- lookup by manufacturer part number
``POST /api/v1/search/keyword``     -- keyword / parametric search

Rate limits on the free tier are strict (roughly 1000 calls/day, 30/minute),
so the provider defaults to 1.5 requests/second and leans on the cache. Mouser
returns ``Errors`` inside a 200 response, which is handled explicitly below --
an invalid key shows up that way rather than as an HTTP 401.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import ProviderError
from ..core.models import PartData
from ..util import log
from ..util.money import normalize_currency
from ..util.text import clean, normalize_mpn
from ..util.units import parse_int
from .base import (
    Provider, build_price_breaks, normalize_compliance_state,
    normalize_lifecycle,
)

LOG = log.get("providers.mouser")

BASE = "https://api.mouser.com/api/v1"


class MouserProvider(Provider):
    id = "mouser"
    default_currency = "USD"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.currency = normalize_currency(self.config.settings.currency, "USD")

    # -- request helper --------------------------------------------------- #

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self.http.post(
            f"{BASE}{path}",
            params={"apiKey": self.credential("api_key")},
            json_body=body,
            headers={"Accept": "application/json"},
            cache_ttl=self.config.settings.http_cache_ttl,
        )
        payload = response.json() or {}
        errors = payload.get("Errors") or []
        if errors:
            messages = "; ".join(
                clean(error.get("Message") or error.get("message"))
                for error in errors if isinstance(error, dict)
            ) or str(errors)[:200]
            lowered = messages.lower()
            if any(word in lowered for word in
                   ("invalid", "unauthor", "not authorized", "api key",
                    "forbidden")):
                raise ProviderError(
                    self.id,
                    f"Mouser rejected the API key: {messages}",
                    retryable=False,
                )
            if "limit" in lowered or "exceeded" in lowered:
                raise ProviderError(
                    self.id, f"Mouser rate/quota limit reached: {messages}")
            LOG.info("Mouser returned a soft error: %s", messages)
        return payload

    # -- lookup ----------------------------------------------------------- #

    def fetch(self, mpn: str, manufacturer: str = "") -> PartData | None:
        payload = self._post("/search/partnumber", {
            "SearchByPartRequest": {
                "mouserPartNumber": clean(mpn),
                # 'Exact' would miss packaging variants; 'None' ranks them all.
                "partSearchOptions": "",
            }
        })
        parts = ((payload.get("SearchResults") or {}).get("Parts")) or []
        if not parts:
            payload = self._post("/search/keyword", {
                "SearchByKeywordRequest": {
                    "keyword": clean(mpn), "records": 10, "startingRecord": 0,
                    "searchOptions": "", "searchWithYourSignUpLanguage": "",
                }
            })
            parts = ((payload.get("SearchResults") or {}).get("Parts")) or []
        if not parts:
            return None

        target = normalize_mpn(mpn)
        target_mfr = clean(manufacturer).lower()

        def score(entry: dict[str, Any]) -> tuple[int, int, int]:
            candidate = normalize_mpn(entry.get("ManufacturerPartNumber"))
            exact = 2 if candidate == target else (
                1 if target and target in candidate else 0)
            mfr = clean(entry.get("Manufacturer")).lower()
            mfr_hit = 1 if target_mfr and target_mfr[:6] in mfr else 0
            stock = parse_int(entry.get("AvailabilityInStock"), 0) or 0
            return exact, mfr_hit, stock

        best = max(parts, key=score)
        return self._to_part(best)

    # -- mapping ---------------------------------------------------------- #

    def _to_part(self, entry: dict[str, Any]) -> PartData:
        lifecycle, note = normalize_lifecycle(
            entry.get("LifecycleStatus"),
            entry.get("ProductStatus"),
        )
        part = PartData(
            mpn=clean(entry.get("ManufacturerPartNumber")),
            manufacturer=clean(entry.get("Manufacturer")),
            description=clean(entry.get("Description")),
            category=clean(entry.get("Category")),
            lifecycle=lifecycle,
            lifecycle_note=clean(note),
            datasheet_url=clean(entry.get("DataSheetUrl")),
            image_url=clean(entry.get("ImagePath")),
            product_url=clean(entry.get("ProductDetailUrl")),
            providers=[self.id],
        )

        for attribute in entry.get("ProductAttributes") or []:
            name = clean(attribute.get("AttributeName"))
            value = clean(attribute.get("AttributeValue"))
            if name and value:
                part.specs.setdefault(name, value)
        part.package = part.specs.get("Package / Case", "") or \
            part.specs.get("Packaging", "")

        compliance = self.compliance_from_specs(part.specs)
        if clean(entry.get("ROHSStatus")):
            compliance.rohs = normalize_compliance_state(entry["ROHSStatus"])
            compliance.rohs_note = clean(entry["ROHSStatus"])
        for item in entry.get("ProductCompliance") or []:
            name = clean(item.get("ComplianceName")).upper()
            value = clean(item.get("ComplianceValue"))
            if not value:
                continue
            if name in ("USHTS", "HTSUS", "HTS"):
                compliance.hts_code = value
            elif name == "ECCN":
                compliance.eccn = value
                if value.upper() not in ("EAR99", ""):
                    compliance.export_controlled = True
            elif name in ("COO", "COUNTRYOFORIGIN"):
                compliance.country_of_origin = value
            elif "REACH" in name:
                compliance.reach = normalize_compliance_state(value)
                compliance.reach_note = value
            elif "TARIC" in name or "CNHTS" in name:
                compliance.hts_code = compliance.hts_code or value
        compliance.sources.append("Mouser")
        part.compliance = compliance

        stock = parse_int(entry.get("AvailabilityInStock"))
        if stock is None:
            stock = self._parse_availability(entry.get("Availability"))
        factory_stock = parse_int(entry.get("FactoryStock"))
        lead_days = self.lead_days(entry.get("LeadTime"))

        currency = self.currency
        breaks = entry.get("PriceBreaks") or []
        if breaks:
            currency = normalize_currency(
                (breaks[0] or {}).get("Currency") or currency, currency)

        offer = self.make_offer(
            sku=clean(entry.get("MouserPartNumber")),
            packaging=clean(entry.get("Reeling") and "Reel" or
                            part.specs.get("Packaging", "")),
            stock=stock,
            factory_stock=factory_stock,
            moq=parse_int(entry.get("Min"), 1),
            spq=parse_int(entry.get("Mult")),
            order_multiple=parse_int(entry.get("Mult")),
            lead_time_days=0 if (stock or 0) > 0 else lead_days,
            price_breaks=build_price_breaks(breaks, currency),
            currency=currency,
            url=clean(entry.get("ProductDetailUrl")),
            region="US",
            authorized=True,
        )
        if offer.price_breaks or offer.stock:
            part.offers.append(offer)

        part.total_avail = stock
        replacement = clean(entry.get("SuggestedReplacement"))
        if replacement and normalize_mpn(replacement) != normalize_mpn(part.mpn):
            part.alternate_mpns.append(replacement)
            if lifecycle.is_risky and not part.lifecycle_note:
                part.lifecycle_note = f"Mouser suggests {replacement}"
        return part

    @staticmethod
    def _parse_availability(value: object) -> int | None:
        """Mouser reports availability as e.g. ``"12480 In Stock"``."""
        text = clean(value)
        if not text:
            return None
        return parse_int(text.split()[0]) if text.split() else None

    # -- search ----------------------------------------------------------- #

    def search(self, query: str, limit: int = 10) -> list[PartData]:
        payload = self._post("/search/keyword", {
            "SearchByKeywordRequest": {
                "keyword": clean(query),
                "records": max(1, min(limit, 50)),
                "startingRecord": 0,
            }
        })
        parts = ((payload.get("SearchResults") or {}).get("Parts")) or []
        return [self._to_part(entry) for entry in parts[:limit]]

    def test_mpn(self) -> str:
        return "GRM188R71C104KA01D"
