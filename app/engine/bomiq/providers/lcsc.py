"""
LCSC provider -- low-cost APAC sourcing.

LCSC has no long-term stable public API. Two transports are supported:

* **Open API** (preferred) -- if you have partner credentials, set the API key
  and secret and, optionally, ``BOMIQ_LCSC_BASE_URL`` to the endpoint LCSC gave
  you. The adapter sends the key as a bearer token.
* **Public catalogue endpoints** -- the same JSON the LCSC website calls. These
  work without a key but are undocumented, rate limited and can change without
  notice, so the provider is disabled by default and every record it returns is
  marked ``unofficial source``.

Because prices come back in CNY or USD depending on the endpoint, the currency
is read per response rather than assumed.
"""

from __future__ import annotations

import os
from typing import Any

from ..core.models import PartData
from ..util import log
from ..util.money import normalize_currency
from ..util.text import clean, normalize_mpn
from ..util.units import parse_int
from .base import (
    Provider, build_price_breaks, normalize_compliance_state,
    normalize_lifecycle,
)

LOG = log.get("providers.lcsc")

PUBLIC_SEARCH = "https://wmsc.lcsc.com/wmsc/search/global"
PUBLIC_DETAIL = "https://wmsc.lcsc.com/wmsc/product/detail"
UNOFFICIAL_NOTE = ("Unofficial LCSC catalogue endpoint — verify before "
                   "committing a purchase order")


class LcscProvider(Provider):
    id = "lcsc"
    default_currency = "USD"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.base_url = clean(os.environ.get("BOMIQ_LCSC_BASE_URL"))
        self.api_key = clean(self.credential("api_key", required=False))
        self.official = bool(self.base_url and self.api_key)

    def check_credentials(self) -> tuple[bool, str]:
        if self.official:
            return True, "Partner API configured"
        return True, ("No key set — using the public catalogue endpoint "
                      "(unofficial, rate limited)")

    # -- transport -------------------------------------------------------- #

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _search(self, term: str, limit: int = 10) -> list[dict[str, Any]]:
        if self.official:
            response = self.http.post(
                f"{self.base_url.rstrip('/')}/product/search",
                json_body={"keyword": clean(term), "pageSize": limit,
                           "currentPage": 1},
                headers=self._headers(),
                cache_ttl=self.config.settings.http_cache_ttl,
                allow_status=(400, 404),
            )
        else:
            response = self.http.post(
                PUBLIC_SEARCH,
                json_body={"keyword": clean(term), "currentPage": 1,
                           "pageSize": limit},
                headers=self._headers(),
                cache_ttl=self.config.settings.http_cache_ttl,
                allow_status=(400, 403, 404, 412),
            )
        if response.status >= 400:
            LOG.info("LCSC search for %r returned HTTP %s", term,
                     response.status)
            return []
        payload = response.json() or {}
        return _extract_products(payload)[:limit]

    # -- lookup ----------------------------------------------------------- #

    def fetch(self, mpn: str, manufacturer: str = "") -> PartData | None:
        products = self._search(clean(mpn))
        if not products:
            return None
        target = normalize_mpn(mpn)

        def score(product: dict[str, Any]) -> tuple[int, int]:
            candidate = normalize_mpn(
                product.get("productModel") or product.get("mpn")
                or product.get("manufacturerPartNumber"))
            exact = 2 if candidate == target else (
                1 if target and target in candidate else 0)
            return exact, parse_int(product.get("stockNumber")
                                    or product.get("stock"), 0) or 0

        best = max(products, key=score)
        if score(best)[0] == 0:
            return None
        return self._to_part(best)

    def _to_part(self, product: dict[str, Any]) -> PartData:
        nested_brand = product.get("brand")
        brand = (
            product.get("brandNameEn")
            or product.get("brandName")
            or ((nested_brand.get("nameEn") or nested_brand.get("name"))
                if isinstance(nested_brand, dict) else "")
        )
        lifecycle, note = normalize_lifecycle(
            product.get("productStatus"), product.get("lifeCycle"),
            product.get("status"),
        )
        part = PartData(
            mpn=clean(product.get("productModel") or product.get("mpn")),
            manufacturer=clean(brand),
            description=clean(product.get("productIntroEn")
                              or product.get("описание")
                              or product.get("description")),
            category=clean(product.get("catalogName")
                           or product.get("parentCatalogName")),
            package=clean(product.get("encapStandard")
                          or product.get("package")),
            lifecycle=lifecycle, lifecycle_note=clean(note),
            datasheet_url=clean(product.get("pdfUrl")
                                or product.get("datasheetUrl")),
            image_url=_first_image(product),
            providers=[self.id],
        )
        code = clean(product.get("productCode"))
        if code:
            part.product_url = f"https://www.lcsc.com/product-detail/{code}.html"
        part.specs["Data source"] = "LCSC" if self.official else UNOFFICIAL_NOTE
        for spec in product.get("paramVOList") or []:
            if isinstance(spec, dict):
                name = clean(spec.get("paramNameEn"))
                value = clean(spec.get("paramValueEn"))
                if name and value:
                    part.specs[name] = value

        compliance = self.compliance_from_specs(part.specs)
        rohs = product.get("isEnvironment") if "isEnvironment" in product \
            else product.get("rohs")
        if rohs is not None:
            compliance.rohs = normalize_compliance_state(
                "compliant" if rohs in (True, 1, "1", "Y", "yes") else str(rohs))
        compliance.country_of_origin = compliance.country_of_origin or "CN"
        compliance.sources.append("LCSC" if self.official else "LCSC (public)")
        part.compliance = compliance

        stock = parse_int(product.get("stockNumber") or product.get("stock"), 0)
        ladder = product.get("productPriceList") or product.get("prices") or []
        # The price field and the currency have to be chosen together: LCSC
        # ships both a local-currency price and a USD one, so taking the USD
        # figure and then converting it as if it were CNY is a 7x error.
        native_currency = normalize_currency(
            product.get("currencyCode") or product.get("currency") or "USD",
            "USD")
        has_usd = any(isinstance(entry, dict) and entry.get("usdPrice")
                      not in (None, "") for entry in ladder)
        if has_usd:
            currency = "USD"
            price_keys = ("usdPrice",)
        else:
            currency = native_currency
            price_keys = ("productPrice", "price", "cost")
        breaks = build_price_breaks(
            ladder, currency,
            qty_keys=("ladder", "quantity", "from", "minQty"),
            price_keys=price_keys)

        offer = self.make_offer(
            distributor="LCSC Electronics",
            sku=code,
            packaging=clean(product.get("encapStandard")),
            stock=stock,
            moq=parse_int(product.get("minBuyNumber")
                          or product.get("minPacketNumber"), 1),
            spq=parse_int(product.get("minPacketNumber")),
            order_multiple=parse_int(product.get("minPacketNumber")),
            lead_time_days=0 if (stock or 0) > 0 else self.lead_days(
                product.get("leadTime")),
            price_breaks=breaks,
            currency=currency,
            url=part.product_url,
            region="APAC",
            authorized=self.official,
            warnings=[] if self.official else [UNOFFICIAL_NOTE],
        )
        if offer.price_breaks or offer.stock:
            part.offers.append(offer)
        part.total_avail = stock
        return part

    def search(self, query: str, limit: int = 10) -> list[PartData]:
        return [self._to_part(product)
                for product in self._search(clean(query), limit=limit)]

    def test_mpn(self) -> str:
        return "RC0603FR-0710KL"


# --------------------------------------------------------------------------- #

def _extract_products(payload: Any) -> list[dict[str, Any]]:
    """Find the product list in any of LCSC's response shapes."""
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, dict):
            for key in ("productSearchResultVO", "data", "result"):
                nested = result.get(key)
                if isinstance(nested, dict):
                    products = nested.get("productList") or nested.get("list")
                    if isinstance(products, list):
                        return [p for p in products if isinstance(p, dict)]
            products = result.get("productList") or result.get("list")
            if isinstance(products, list):
                return [p for p in products if isinstance(p, dict)]
            if result.get("productModel") or result.get("productCode"):
                return [result]
        for key in ("data", "products", "productList", "list"):
            value = payload.get(key)
            if isinstance(value, list):
                return [p for p in value if isinstance(p, dict)]
            if isinstance(value, dict):
                found = _extract_products(value)
                if found:
                    return found
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    return []


def _first_image(product: dict[str, Any]) -> str:
    images = product.get("productImages") or product.get("images") or []
    if isinstance(images, list) and images:
        first = images[0]
        return clean(first if isinstance(first, str) else first.get("url"))
    return clean(product.get("productImageUrl"))
