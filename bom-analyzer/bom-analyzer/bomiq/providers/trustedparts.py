"""
TrustedParts provider -- authorised-distributor stock aggregation.

TrustedParts (an ECIA initiative) aggregates inventory from authorised
distributors only, which makes it useful for hunting a scarce part without
straying into the grey market.

There is no openly published REST contract, so this adapter is written against
the partner JSON feed and is configurable:

* ``BOMIQ_TRUSTEDPARTS_BASE_URL`` -- the endpoint your partner agreement gives
  you (defaults to ``https://api.trustedparts.com/v1``)
* API key from Settings, sent as ``X-API-Key`` and as ``apiKey``

The response parser is schema-tolerant: it looks for the usual containers and
otherwise scans for objects carrying a part number and a distributor name. If
the shape does not match, the provider reports a clear message and the engine
carries on with the other providers rather than failing the analysis.

Disabled by default.
"""

from __future__ import annotations

import os
from typing import Any, Iterator

from ..core.errors import ProviderError
from ..core.models import PartData
from ..util import log
from ..util.money import normalize_currency
from ..util.text import clean, normalize_mpn
from ..util.units import parse_int
from .base import Provider, build_price_breaks, normalize_lifecycle

LOG = log.get("providers.trustedparts")

DEFAULT_BASE = "https://api.trustedparts.com/v1"


class TrustedPartsProvider(Provider):
    id = "trustedparts"
    default_currency = "USD"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.base_url = clean(
            os.environ.get("BOMIQ_TRUSTEDPARTS_BASE_URL")) or DEFAULT_BASE
        self.api_key = clean(self.credential("api_key", required=False))

    def check_credentials(self) -> tuple[bool, str]:
        if self.api_key:
            return True, "API key present"
        return False, ("TrustedParts needs a partner API key. Request one at "
                       "trustedparts.com, then add it in Settings.")

    def fetch(self, mpn: str, manufacturer: str = "") -> PartData | None:
        if not self.api_key:
            raise ProviderError(
                self.id,
                "TrustedParts is enabled but no API key is configured.",
                retryable=False,
            )
        response = self.http.get(
            f"{self.base_url.rstrip('/')}/parts/search",
            params={
                "partNumber": clean(mpn),
                "manufacturer": clean(manufacturer) or None,
                "apiKey": self.api_key,
                "authorizedOnly": "true",
            },
            headers={"Accept": "application/json", "X-API-Key": self.api_key},
            cache_ttl=self.config.settings.http_cache_ttl,
            allow_status=(400, 404),
        )
        if response.status in (400, 404):
            return None
        payload = response.json() or {}
        records = list(_iter_records(payload))
        if not records:
            return None

        target = normalize_mpn(mpn)
        grouped = [
            record for record in records
            if not target or target in normalize_mpn(
                record.get("partNumber") or record.get("mpn"))
        ] or records

        first = grouped[0]
        lifecycle, note = normalize_lifecycle(
            first.get("lifecycleStatus"), first.get("status"))
        part = PartData(
            mpn=clean(first.get("partNumber") or first.get("mpn")),
            manufacturer=clean(first.get("manufacturer")
                               or first.get("manufacturerName")),
            description=clean(first.get("description")),
            category=clean(first.get("category")),
            lifecycle=lifecycle, lifecycle_note=clean(note),
            datasheet_url=clean(first.get("datasheetUrl")),
            product_url=clean(first.get("url")),
            providers=[self.id],
        )
        part.specs["Data source"] = "TrustedParts (authorised distributors)"
        part.compliance.sources.append("TrustedParts")

        for record in grouped:
            distributor = clean(record.get("distributor")
                                or record.get("distributorName")
                                or record.get("sellerName"))
            if not distributor:
                continue
            currency = normalize_currency(
                record.get("currency") or self.default_currency,
                self.default_currency)
            stock = parse_int(record.get("quantityAvailable")
                              or record.get("stock")
                              or record.get("inventory"), 0)
            breaks = build_price_breaks(
                record.get("priceBreaks") or record.get("prices") or [],
                currency,
                qty_keys=("quantity", "breakQuantity", "minQty", "from"),
                price_keys=("price", "unitPrice", "cost"))
            offer = self.make_offer(
                distributor=distributor,
                sku=clean(record.get("distributorPartNumber")
                          or record.get("sku")),
                packaging=clean(record.get("packaging")),
                stock=stock,
                moq=parse_int(record.get("minimumOrderQuantity"), 1),
                spq=parse_int(record.get("standardPackQuantity")),
                lead_time_days=0 if (stock or 0) > 0 else self.lead_days(
                    record.get("leadTime"), record.get("factoryLeadTime")),
                price_breaks=breaks,
                currency=currency,
                url=clean(record.get("buyUrl") or record.get("url")),
                region=clean(record.get("region")) or "global",
                authorized=True,
            )
            if offer.price_breaks or offer.stock:
                part.offers.append(offer)

        if not part.offers and not part.mpn:
            return None
        part.total_avail = part.total_stock or None
        return part

    def test_mpn(self) -> str:
        return "LM358DR"


_RECORD_MARKERS = ("partNumber", "mpn", "manufacturerPartNumber")


def _iter_records(payload: Any, depth: int = 0) -> Iterator[dict[str, Any]]:
    if depth > 7:
        return
    if isinstance(payload, dict):
        if any(clean(payload.get(marker)) for marker in _RECORD_MARKERS):
            yield payload
            return
        for value in payload.values():
            yield from _iter_records(value, depth + 1)
    elif isinstance(payload, list):
        for value in payload:
            yield from _iter_records(value, depth + 1)
