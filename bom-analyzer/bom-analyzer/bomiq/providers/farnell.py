"""
Farnell / element14 / Newark provider -- Product Search REST API.

Endpoint
--------
``GET https://api.element14.com/catalog/products``

Key parameters:

``term``                            ``manuPartNum:<MPN>`` or ``any:<text>``
``storeInfo.id``                    regional store, e.g. ``uk.farnell.com``,
                                    ``in.element14.com``, ``www.newark.com``
``resultsSettings.responseGroup``   ``large`` to get prices, stock and attributes
``callInfo.apiKey``                 your partner API key

The store determines both the catalogue and the currency, so BOM-IQ picks a
sensible default from the configured region and lets the user override it.
Disabled by default; enable it in Settings once you have a partner key.
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

LOG = log.get("providers.farnell")

BASE = "https://api.element14.com/catalog/products"

STORE_CURRENCY = {
    "uk.farnell.com": "GBP", "ie.farnell.com": "EUR", "de.farnell.com": "EUR",
    "fr.farnell.com": "EUR", "it.farnell.com": "EUR", "es.farnell.com": "EUR",
    "nl.farnell.com": "EUR", "be.farnell.com": "EUR", "at.farnell.com": "EUR",
    "dk.farnell.com": "DKK", "se.farnell.com": "SEK", "no.farnell.com": "NOK",
    "pl.farnell.com": "PLN", "cz.farnell.com": "CZK", "hu.farnell.com": "HUF",
    "ch.farnell.com": "CHF", "il.farnell.com": "ILS", "tr.farnell.com": "TRY",
    "in.element14.com": "INR", "sg.element14.com": "SGD",
    "my.element14.com": "MYR", "th.element14.com": "THB",
    "ph.element14.com": "PHP", "vn.element14.com": "VND",
    "cn.element14.com": "CNY", "hk.element14.com": "HKD",
    "tw.element14.com": "TWD", "kr.element14.com": "KRW",
    "au.element14.com": "AUD", "nz.element14.com": "NZD",
    "www.newark.com": "USD", "canada.newark.com": "CAD",
    "mexico.newark.com": "MXN",
}

REGION_DEFAULT_STORE = {
    "global": "uk.farnell.com",
    "emea": "uk.farnell.com",
    "apac": "sg.element14.com",
    "india": "in.element14.com",
    "americas": "www.newark.com",
    "us": "www.newark.com",
}


class FarnellProvider(Provider):
    id = "farnell"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        configured = clean(self.credential("store", required=False))
        region = clean(self.config.settings.target_region).lower()
        self.store = configured or REGION_DEFAULT_STORE.get(
            region, "uk.farnell.com")
        self.default_currency = STORE_CURRENCY.get(self.store, "GBP")

    @property
    def name(self) -> str:
        label = "Newark" if "newark" in self.store else (
            "element14" if "element14" in self.store else "Farnell")
        return f"{label} ({self.store})"

    # -- lookup ----------------------------------------------------------- #

    def _query(self, term: str, limit: int = 10) -> list[dict[str, Any]]:
        response = self.http.get(
            BASE,
            params={
                "term": term,
                "storeInfo.id": self.store,
                "resultsSettings.offset": 0,
                "resultsSettings.numberOfResults": max(1, min(limit, 25)),
                "resultsSettings.responseGroup": "large",
                "callInfo.responseDataFormat": "json",
                "callInfo.apiKey": self.credential("api_key"),
            },
            headers={"Accept": "application/json"},
            cache_ttl=self.config.settings.http_cache_ttl,
            allow_status=(400, 404),
        )
        if response.status in (400, 404):
            body = response.text[:200]
            if "apiKey" in body or "authoriz" in body.lower():
                raise ProviderError(
                    self.id,
                    "element14 rejected the API key. Check it in the partner "
                    "portal and confirm the store is enabled for your account.",
                    retryable=False,
                )
            return []
        payload = response.json() or {}
        for key in ("manufacturerPartNumberSearchReturn",
                    "premierFarnellPartNumberReturn",
                    "keywordSearchReturn"):
            block = payload.get(key)
            if isinstance(block, dict):
                products = block.get("products") or []
                if isinstance(products, list):
                    return [p for p in products if isinstance(p, dict)]
        return []

    def fetch(self, mpn: str, manufacturer: str = "") -> PartData | None:
        products = self._query(f"manuPartNum:{clean(mpn)}")
        if not products:
            products = self._query(f"any:{clean(mpn)}")
        if not products:
            return None
        target = normalize_mpn(mpn)

        def score(product: dict[str, Any]) -> tuple[int, int]:
            candidate = normalize_mpn(
                product.get("translatedManufacturerPartNumber")
                or product.get("manufacturerPartNumber"))
            exact = 2 if candidate == target else (
                1 if target and target in candidate else 0)
            stock = parse_int((product.get("stock") or {}).get("level"), 0) or 0
            return exact, stock

        return self._to_part(max(products, key=score))

    # -- mapping ---------------------------------------------------------- #

    def _to_part(self, product: dict[str, Any]) -> PartData:
        attributes: dict[str, str] = {}
        for attribute in product.get("attributes") or []:
            if isinstance(attribute, dict):
                name = clean(attribute.get("attributeLabel"))
                value = clean(attribute.get("attributeValue"))
                unit = clean(attribute.get("attributeUnit"))
                if name and value:
                    attributes[name] = f"{value} {unit}".strip()

        lifecycle, note = normalize_lifecycle(
            product.get("productStatus"),
            attributes.get("Lifecycle Status"),
            attributes.get("Product Status"),
        )

        part = PartData(
            mpn=clean(product.get("translatedManufacturerPartNumber")
                      or product.get("manufacturerPartNumber")),
            manufacturer=clean(product.get("brandName")
                               or product.get("vendorName")),
            description=clean(product.get("displayName")
                              or product.get("translatedPrimaryDescription")),
            category=clean((product.get("category") or {}).get("name")
                           if isinstance(product.get("category"), dict)
                           else product.get("category")),
            lifecycle=lifecycle, lifecycle_note=clean(note),
            specs=attributes,
            providers=[self.id],
        )
        datasheets = product.get("datasheets") or []
        if isinstance(datasheets, list) and datasheets:
            first = datasheets[0]
            part.datasheet_url = clean(first.get("url")
                                       if isinstance(first, dict) else first)
        images = product.get("image") or {}
        if isinstance(images, dict) and images.get("baseName"):
            part.image_url = clean(
                f"https://{self.store}/productimages/standard/en_GB/"
                f"{images['baseName']}")
        sku = clean(product.get("sku"))
        if sku:
            part.product_url = f"https://{self.store}/w/search?st={sku}"
        part.package = attributes.get("Package / Case", "") or \
            attributes.get("Case Style", "")

        compliance = self.compliance_from_specs(attributes)
        rohs_code = clean(product.get("rohsStatusCode"))
        if rohs_code:
            compliance.rohs = normalize_compliance_state(
                "compliant" if rohs_code in ("1", "2", "7", "RC")
                else rohs_code)
            compliance.rohs_note = f"element14 RoHS code {rohs_code}"
        compliance.country_of_origin = compliance.country_of_origin or clean(
            product.get("countryOfOrigin"))
        compliance.sources.append(self.name)
        part.compliance = compliance

        stock_block = product.get("stock") or {}
        stock = parse_int(stock_block.get("level"), 0)
        lead_days = self.lead_days(stock_block.get("leastLeadTime"),
                                   stock_block.get("leadTime"))
        currency = normalize_currency(
            product.get("currency") or self.default_currency,
            self.default_currency)

        breaks = build_price_breaks(
            product.get("prices") or [], currency,
            qty_keys=("from", "From", "quantity"),
            price_keys=("cost", "Cost", "price"))

        offer = self.make_offer(
            distributor=self.name,
            sku=sku,
            packaging=clean(product.get("packSize") and
                            f"Pack of {product['packSize']}" or ""),
            stock=stock,
            moq=parse_int(product.get("translatedMinimumOrderQuality"), 1),
            spq=parse_int(product.get("packSize")),
            order_multiple=parse_int(product.get("packSize")),
            lead_time_days=0 if (stock or 0) > 0 else lead_days,
            price_breaks=breaks,
            currency=currency,
            url=part.product_url,
            region="EMEA" if "farnell" in self.store else "APAC"
            if "element14" in self.store else "Americas",
            authorized=True,
        )
        if offer.price_breaks or offer.stock:
            part.offers.append(offer)
        part.total_avail = stock
        return part

    # -- search ----------------------------------------------------------- #

    def search(self, query: str, limit: int = 10) -> list[PartData]:
        products = self._query(f"any:{clean(query)}", limit=limit)
        return [self._to_part(product) for product in products[:limit]]

    def test_mpn(self) -> str:
        return "GRM188R71C104KA01D"
