"""Nexar (Octopart) GraphQL adapter (live when OAuth2 creds are set).

Uses the OAuth2 client_credentials flow against the Nexar Identity
Service and queries the GraphQL ``supSearchMpn`` operation with an
exact-MPN match preference. On the free "Evaluation" plan this returns
matched parts with distributor offers, prices and inventory levels;
datasheets and tech-spec/lifecycle fields are add-on gated and are
deliberately not requested so the query works on every plan.
"""

from __future__ import annotations

import time as _time
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
    parse_float,
    parse_int,
)

_TOKEN_URL = "https://identity.nexar.com/connect/token"
_GRAPHQL_URL = "https://api.nexar.com/graphql"

_QUERY = """\
query partsByMpn($q: String!, $country: String!, $currency: String!, $limit: Int!) {
  supSearchMpn(q: $q, country: $country, currency: $currency, limit: $limit) {
    hits
    results {
      description
      part {
        mpn
        shortDescription
        octopartUrl
        totalAvail
        estimatedFactoryLeadDays
        medianPrice1000 {
          quantity
          price
          currency
          convertedPrice
          convertedCurrency
        }
        category {
          name
        }
        manufacturer {
          name
        }
        sellers {
          company {
            name
          }
          offers {
            inventoryLevel
            moq
            factoryLeadDays
            clickUrl
            prices {
              quantity
              price
              currency
              convertedPrice
              convertedCurrency
            }
          }
        }
      }
    }
  }
}
"""

_token: str = ""
_token_expires: float = 0.0


def reset_token_cache() -> None:
    """Drop the module-level OAuth2 token so a new credential set starts clean."""
    global _token, _token_expires
    _token = ""
    _token_expires = 0.0


class NexarAdapter(SourceAdapter):
    name = "Nexar"
    source_type = SourceType.DISTRIBUTOR

    def __init__(self) -> None:
        _bucket_cfg = config_section("rate_limits")
        self._bucket = TokenBucket(float(_bucket_cfg.get("nexar_per_minute", 30)))

    @property
    def available(self) -> bool:
        s = get_settings()
        return bool(s.nexar_client_id and s.nexar_client_secret)

    async def search(self, mpn: str, manufacturer: str, region: str, currency: str) -> SourceResult:
        if not self.available:
            return SourceResult(
                source_name=self.name,
                source_type=self.source_type,
                source_url=_GRAPHQL_URL,
                retrieved_at=datetime.now(UTC),
                region=region,
                currency=currency,
                confidence=0.0,
                errors=["Nexar credentials not configured — using demo data instead."],
            )
        await self._bucket.acquire()
        timeout = config_section("orchestrator").get("timeout_seconds", 25)
        async with httpx.AsyncClient(timeout=timeout) as client:
            token, tok_error = await self._ensure_token(client)
            if token is None:
                return SourceResult(
                    source_name=self.name,
                    source_type=self.source_type,
                    source_url=_TOKEN_URL,
                    retrieved_at=datetime.now(UTC),
                    region=region,
                    currency=currency,
                    confidence=0.0,
                    errors=[tok_error or "Nexar OAuth2 token request failed."],
                )
            resp = await client.post(
                _GRAPHQL_URL,
                json={
                    "query": _QUERY,
                    "variables": {
                        "q": mpn,
                        "country": region,
                        "currency": currency,
                        "limit": 5,
                    },
                },
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
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
                    errors=[f"Nexar HTTP {resp.status_code}: {resp.text[:300]}"],
                )
            body = resp.json()
            try:
                result = self._first_result(body, mpn)
            except NexarGraphQLError as exc:
                return SourceResult(
                    source_name=self.name,
                    source_type=self.source_type,
                    source_url=str(resp.url),
                    retrieved_at=datetime.now(UTC),
                    region=region,
                    currency=currency,
                    confidence=0.0,
                    errors=[str(exc)],
                )
            if result is None:
                return SourceResult(
                    source_name=self.name,
                    source_type=self.source_type,
                    source_url=str(resp.url),
                    retrieved_at=datetime.now(UTC),
                    region=region,
                    currency=currency,
                    confidence=0.0,
                    errors=[f"Nexar: no results for {mpn}."],
                )
            return self._to_result(result, str(resp.url), region, currency)

    async def _ensure_token(self, client: httpx.AsyncClient) -> tuple[str | None, str]:
        global _token, _token_expires
        if _token and _time.monotonic() < _token_expires - 60:
            return _token, ""
        s = get_settings()
        resp = await client.post(
            _TOKEN_URL,
            data={
                "client_id": s.nexar_client_id,
                "client_secret": s.nexar_client_secret,
                "grant_type": "client_credentials",
                "scope": "supply.domain",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            return None, f"Nexar OAuth2 token request failed (HTTP {resp.status_code}): {resp.text[:300]}"
        data = resp.json()
        _token = data.get("access_token", "")
        _token_expires = _time.monotonic() + float(data.get("expires_in", 3600))
        return (_token or None), ""

    def _first_result(self, body: dict[str, Any], mpn: str) -> dict[str, Any] | None:
        errors = body.get("errors")
        if errors:
            raise NexarGraphQLError(errors[0].get("message") if isinstance(errors, list) else str(errors))
        results = ((body.get("data") or {}).get("supSearchMpn") or {}).get("results") or []
        exact = next(
            (r for r in results if ((r.get("part") or {}).get("mpn") or "").upper() == mpn.upper()),
            None,
        )
        return exact or (results[0] if results else None)

    def _to_result(self, result: dict[str, Any] | None, url: str, region: str, currency: str) -> SourceResult:
        if result is None:
            return SourceResult(
                source_name=self.name,
                source_type=self.source_type,
                source_url=url,
                retrieved_at=datetime.now(UTC),
                region=region,
                currency=currency,
                confidence=0.0,
                errors=["Nexar: no results found."],
            )
        part = result.get("part") or {}
        mfr = (part.get("manufacturer") or {}).get("name", "")
        mfr_mpn = part.get("mpn") or ""
        desc = part.get("shortDescription") or result.get("description") or ""
        total_avail = parse_int(part.get("totalAvail"))
        lead_days = _lead_days(part.get("estimatedFactoryLeadDays"))
        offers: list[Offer] = []
        offers_seen: set[str] = set()
        for seller in part.get("sellers") or []:
            company = ((seller.get("company") or {}).get("name") or "").strip()
            if not company or company.lower() in offers_seen:
                continue
            offers_seen.add(company.lower())
            offer = self._seller_offer(seller, lead_days, total_avail, region, currency)
            if offer is not None:
                offers.append(offer)
        facts = PartFacts(
            manufacturer=mfr,
            manufacturer_mpn=mfr_mpn,
            description=desc,
            category=(part.get("category") or {}).get("name", ""),
            lifecycle_status=LifecycleStatus.UNKNOWN.value,
            product_url=part.get("octopartUrl") or "",
        )
        return SourceResult(
            source_name=self.name,
            source_type=self.source_type,
            source_url=url,
            retrieved_at=datetime.now(UTC),
            region=region,
            currency=currency,
            confidence=0.85,
            facts=facts,
            offers=offers,
        )

    def _seller_offer(
        self, seller: dict[str, Any], part_lead: str, total_avail: int | None, region: str, currency: str
    ) -> Offer | None:
        offers = seller.get("offers") or []
        if not offers:
            return None
        first = offers[0]
        breaks_raw: list[tuple[int | None, float | None, str]] = []
        for price in first.get("prices") or []:
            qty = parse_int(price.get("quantity"))
            val = _price(price, currency)
            if qty is not None and val is not None:
                breaks_raw.append((qty, val, price.get("convertedCurrency") or price.get("currency") or currency))
        if not breaks_raw:
            return None
        breaks = [PriceBreak(qty=q, price=p, currency=cur) for q, p, cur in breaks_raw]
        breaks.sort(key=lambda b: b.qty)
        stock = parse_int(first.get("inventoryLevel"))
        if stock is None:
            stock = total_avail
        lead = _lead_days(first.get("factoryLeadDays")) or part_lead
        return Offer(
            distributor=((seller.get("company") or {}).get("name") or "").strip(),
            available=bool(breaks),
            available_qty=stock,
            price_breaks=breaks,
            moq=parse_int(first.get("moq")),
            lead_time=lead,
            currency=breaks[0].currency,
            region=region,
            url=first.get("clickUrl") or "",
        )


def _price(price: dict[str, Any], fallback_currency: str) -> float | None:
    val = parse_float(price.get("convertedPrice"))
    if val is not None:
        return _round(val)
    val = parse_float(price.get("price"))
    return _round(val) if val is not None else None


def _round(value: float) -> float:
    return round(value, 4)


def _lead_days(value) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):g} days"
    except (TypeError, ValueError):
        return str(value)


class NexarGraphQLError(Exception):
    """Raised when the GraphQL response reports a query-level error."""
