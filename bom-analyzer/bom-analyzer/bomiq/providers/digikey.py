"""
DigiKey provider -- Product Information API **v4**.

Authentication
--------------
OAuth2 *client credentials*: ``POST {host}/v1/oauth2/token`` with
``grant_type=client_credentials``. The token lasts ~10 minutes, is cached in
memory and refreshed a minute before expiry under a lock so concurrent worker
threads only ever fetch it once.

Endpoints used
--------------
``POST /products/v4/search/keyword``               -- lookup by MPN
``GET  /products/v4/search/{mpn}/productdetails``  -- exact product details
``GET  /products/v4/search/{mpn}/substitutions``   -- manufacturer substitutes

Set ``digikey_sandbox`` in settings to target ``sandbox-api.digikey.com``.
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal
from typing import Any

from ..core.errors import ProviderError
from ..core.models import Lifecycle, PartData
from ..util import log
from ..util.http import HttpError
from ..util.money import normalize_currency, to_decimal
from ..util.text import clean, normalize_mpn
from ..util.units import parse_int
from .base import Provider, build_price_breaks, normalize_lifecycle

LOG = log.get("providers.digikey")

PROD_HOST = "https://api.digikey.com"
SANDBOX_HOST = "https://sandbox-api.digikey.com"


class DigiKeyProvider(Provider):
    id = "digikey"
    default_currency = "USD"
    can_suggest_alternates = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._token = ""
        self._token_expiry = 0.0
        self.host = SANDBOX_HOST if self.config.settings.digikey_sandbox \
            else PROD_HOST
        self.locale_site = "US"
        self.locale_language = "en"
        self.currency = normalize_currency(self.config.settings.currency, "USD")

    # -- auth ------------------------------------------------------------- #

    def _access_token(self) -> str:
        with self._auth_lock:
            if self._token and time.monotonic() < self._token_expiry:
                return self._token
            client_id = self.credential("client_id")
            client_secret = self.credential("client_secret")
            response = self.http.post(
                f"{self.host}/v1/oauth2/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "client_credentials",
                },
                headers={"Accept": "application/json"},
                cache_ttl=0, retries=2,
            )
            payload = response.json() or {}
            token = clean(payload.get("access_token"))
            if not token:
                raise ProviderError(
                    self.id,
                    "DigiKey did not return an access token. Check the Client "
                    "ID and Client Secret, and that the app is subscribed to "
                    "the Product Information API.",
                    retryable=False,
                )
            expires_in = parse_int(payload.get("expires_in"), 600) or 600
            self._token = token
            self._token_expiry = time.monotonic() + max(60, expires_in - 60)
            LOG.info("DigiKey token acquired (valid %ss)", expires_in)
            return self._token

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "X-DIGIKEY-Client-Id": self.credential("client_id"),
            "X-DIGIKEY-Locale-Site": self.locale_site,
            "X-DIGIKEY-Locale-Language": self.locale_language,
            "X-DIGIKEY-Locale-Currency": self.currency,
            "Accept": "application/json",
        }
        customer_id = self.credential("customer_id", required=False)
        if customer_id:
            headers["X-DIGIKEY-Customer-Id"] = customer_id
        return headers

    # -- lookup ----------------------------------------------------------- #

    def fetch(self, mpn: str, manufacturer: str = "") -> PartData | None:
        ttl = self.config.settings.http_cache_ttl
        product = self._product_details(mpn, ttl)
        if product is None:
            product = self._keyword_search_best(mpn, manufacturer, ttl)
        if product is None:
            return None
        return self._to_part(product)

    def _product_details(self, mpn: str, ttl: int) -> dict[str, Any] | None:
        from urllib.parse import quote

        try:
            response = self.http.get(
                f"{self.host}/products/v4/search/{quote(clean(mpn), safe='')}"
                f"/productdetails",
                headers=self._headers(), cache_ttl=ttl, allow_status=(404,),
            )
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise
        if response.status == 404:
            return None
        payload = response.json() or {}
        product = payload.get("Product") or payload.get("product")
        return product if isinstance(product, dict) else None

    def _keyword_search_best(self, mpn: str, manufacturer: str, ttl: int
                             ) -> dict[str, Any] | None:
        response = self.http.post(
            f"{self.host}/products/v4/search/keyword",
            json_body={
                "Keywords": clean(mpn),
                "Limit": 10,
                "Offset": 0,
                "FilterOptionsRequest": {},
            },
            headers=self._headers(), cache_ttl=ttl, allow_status=(404,),
        )
        if response.status == 404:
            return None
        payload = response.json() or {}
        products = payload.get("Products") or []
        if not products:
            return None
        target_mpn = normalize_mpn(mpn)
        target_mfr = clean(manufacturer).lower()

        def score(product: dict[str, Any]) -> tuple[int, int, int]:
            candidate = normalize_mpn(product.get("ManufacturerProductNumber"))
            mfr_name = clean(
                (product.get("Manufacturer") or {}).get("Name")).lower()
            exact = 2 if candidate == target_mpn else (
                1 if target_mpn and target_mpn in candidate else 0)
            mfr_hit = 1 if target_mfr and target_mfr[:6] in mfr_name else 0
            stock = parse_int(product.get("QuantityAvailable"), 0) or 0
            return exact, mfr_hit, stock

        best = max(products, key=score)
        return best if score(best)[0] > 0 else best

    # -- mapping ---------------------------------------------------------- #

    def _to_part(self, product: dict[str, Any]) -> PartData:
        description_block = product.get("Description") or {}
        classifications = product.get("Classifications") or {}
        status = (product.get("ProductStatus") or {}).get("Status")

        lifecycle, note = normalize_lifecycle(
            status,
            "Obsolete" if product.get("Discontinued") else "",
            "End of life" if product.get("EndOfLife") else "",
            "NRND" if product.get("NotRecommendedForNewDesign") else "",
        )
        if lifecycle is Lifecycle.UNKNOWN and product.get("NormallyStocking"):
            lifecycle, note = Lifecycle.ACTIVE, "Normally stocking"

        part = PartData(
            mpn=clean(product.get("ManufacturerProductNumber")),
            manufacturer=clean((product.get("Manufacturer") or {}).get("Name")),
            description=clean(description_block.get("ProductDescription")
                              or description_block.get("DetailedDescription")),
            category=clean((product.get("Category") or {}).get("Name")),
            series=clean((product.get("Series") or {}).get("Name")),
            lifecycle=lifecycle,
            lifecycle_note=clean(note) or clean(status),
            datasheet_url=clean(product.get("DatasheetUrl")),
            image_url=clean(product.get("PhotoUrl")),
            product_url=clean(product.get("ProductUrl")),
            providers=[self.id],
        )

        for parameter in product.get("Parameters") or []:
            name = clean(parameter.get("ParameterText") or parameter.get("Parameter"))
            value = clean(parameter.get("ValueText") or parameter.get("Value"))
            if name and value:
                part.specs[name] = value
        if not part.package:
            part.package = part.specs.get("Package / Case", "") or \
                part.specs.get("Supplier Device Package", "")

        compliance = self.compliance_from_specs(part.specs)
        from .base import normalize_compliance_state

        if clean(classifications.get("RohsStatus")):
            compliance.rohs = normalize_compliance_state(
                classifications["RohsStatus"])
            compliance.rohs_note = clean(classifications["RohsStatus"])
        if clean(classifications.get("ReachStatus")):
            compliance.reach = normalize_compliance_state(
                classifications["ReachStatus"])
            compliance.reach_note = clean(classifications["ReachStatus"])
        compliance.msl = compliance.msl or clean(
            classifications.get("MoistureSensitivityLevel"))
        compliance.eccn = clean(classifications.get("ExportControlClassNumber"))
        compliance.hts_code = clean(classifications.get("HtsusCode"))
        if compliance.eccn and compliance.eccn.upper() not in ("EAR99", ""):
            compliance.export_controlled = True
        compliance.sources.append("DigiKey")
        part.compliance = compliance

        lead_weeks = parse_int(product.get("ManufacturerLeadWeeks"))
        if lead_weeks:
            part.estimated_factory_lead_days = lead_weeks * 7
        part.total_avail = parse_int(product.get("QuantityAvailable"))
        median = to_decimal(product.get("UnitPrice"))
        if median and median > 0:
            part.median_price_1k = median
            part.median_price_1k_currency = self.currency

        variations = product.get("ProductVariations") or []
        if variations:
            for variation in variations:
                offer = self._variation_to_offer(variation, product)
                if offer is not None:
                    part.offers.append(offer)
        else:
            offer = self.make_offer(
                sku=clean(product.get("DigiKeyProductNumber")),
                stock=parse_int(product.get("QuantityAvailable"), 0),
                moq=parse_int(product.get("MinimumOrderQuantity"), 1),
                price_breaks=build_price_breaks(
                    product.get("StandardPricing") or [], self.currency),
                currency=self.currency,
                url=clean(product.get("ProductUrl")),
                lead_time_days=self.lead_days(
                    product.get("ManufacturerLeadWeeks")),
                region="US",
            )
            if offer.price_breaks or offer.stock:
                part.offers.append(offer)

        for name in product.get("OtherNames") or []:
            alias = clean(name)
            if alias and normalize_mpn(alias) != normalize_mpn(part.mpn):
                part.alternate_mpns.append(alias)
        return part

    def _variation_to_offer(self, variation: dict[str, Any],
                            product: dict[str, Any]) -> Any:
        breaks = build_price_breaks(
            variation.get("StandardPricing") or [], self.currency)
        stock = parse_int(variation.get("QuantityAvailableforPackageType"))
        if stock is None:
            stock = parse_int(product.get("QuantityAvailable"), 0)
        packaging = clean((variation.get("PackageType") or {}).get("Name"))
        if not breaks and not stock:
            return None
        return self.make_offer(
            sku=clean(variation.get("DigiKeyProductNumber")),
            packaging=packaging,
            stock=stock,
            moq=parse_int(variation.get("MinimumOrderQuantity"), 1),
            spq=parse_int(variation.get("StandardPackage"))
            or parse_int(variation.get("MultipleQuantity")),
            order_multiple=parse_int(variation.get("MultipleQuantity")),
            price_breaks=breaks,
            currency=self.currency,
            url=clean(product.get("ProductUrl")),
            lead_time_days=0 if (stock or 0) > 0 else self.lead_days(
                product.get("ManufacturerLeadWeeks")),
            region="US",
            authorized=True,
        )

    # -- search / substitutes --------------------------------------------- #

    def search(self, query: str, limit: int = 10) -> list[PartData]:
        response = self.http.post(
            f"{self.host}/products/v4/search/keyword",
            json_body={"Keywords": clean(query), "Limit": max(1, min(limit, 50)),
                       "Offset": 0},
            headers=self._headers(),
            cache_ttl=self.config.settings.http_cache_ttl,
            allow_status=(404,),
        )
        if response.status == 404:
            return []
        payload = response.json() or {}
        return [self._to_part(product)
                for product in (payload.get("Products") or [])[:limit]]

    def alternates(self, part: PartData, limit: int = 10) -> list[PartData]:
        from urllib.parse import quote

        try:
            response = self.http.get(
                f"{self.host}/products/v4/search/"
                f"{quote(clean(part.mpn), safe='')}/substitutions",
                params={"IncludeAllSubstitutes": "true"},
                headers=self._headers(),
                cache_ttl=self.config.settings.http_cache_ttl,
                allow_status=(404,),
            )
        except HttpError as exc:
            if exc.status in (404, 400):
                return []
            raise
        if response.status == 404:
            return []
        payload = response.json() or {}
        entries = payload.get("ProductSubstitutes") or \
            payload.get("Substitutes") or []
        out: list[PartData] = []
        for entry in entries[:limit]:
            product = entry.get("Product") if isinstance(entry, dict) else None
            if isinstance(product, dict):
                out.append(self._to_part(product))
            elif isinstance(entry, dict) and entry.get(
                    "ManufacturerProductNumber"):
                out.append(self._to_part(entry))
        return out

    def test_mpn(self) -> str:
        return "RC0603FR-0710KL"
