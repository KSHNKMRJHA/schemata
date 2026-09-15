"""DigiKey Product Information v4 adapter (live when OAuth2 creds are set).

Uses the OAuth2 2-legged client_credentials flow and caches the access
token locally for its lifetime. KeywordSearch is used with an exact
manufacturer part-number match preference.
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

_TOKEN_URL_PROD = "https://api.digikey.com/v1/oauth2/token"
_BASE_PROD = "https://api.digikey.com"
_TOKEN_URL_SANDBOX = "https://sandbox-api.digikey.com/v1/oauth2/token"
_BASE_SANDBOX = "https://sandbox-api.digikey.com"
_STATUS_MAP = {
    "active": LifecycleStatus.ACTIVE.value,
    "obsolete": LifecycleStatus.OBSOLETE.value,
    "discontinued": LifecycleStatus.OBSOLETE.value,
    "nrdn": LifecycleStatus.NRND.value,
    "nrnd": LifecycleStatus.NRND.value,
    "not recommended": LifecycleStatus.NRND.value,
    "last time buy": LifecycleStatus.LAST_TIME_BUY.value,
    "lasttimebuy": LifecycleStatus.LAST_TIME_BUY.value,
}
_HEADERS = {
    "X-DIGIKEY-Locale-Site": "US",
    "X-DIGIKEY-Locale-Language": "en",
    "X-DIGIKEY-Locale-Currency": "USD",
    "X-DIGIKEY-Customer-Id": "0",
}

_token: str = ""
_token_expires: float = 0.0


def reset_token_cache() -> None:
    """Drop the module-level OAuth2 token so a new credential set starts clean."""
    global _token, _token_expires
    _token = ""
    _token_expires = 0.0


class DigiKeyAdapter(SourceAdapter):
    name = "DigiKey"
    source_type = SourceType.DISTRIBUTOR

    def __init__(self) -> None:
        _bucket_cfg = config_section("rate_limits")
        self._bucket = TokenBucket(float(_bucket_cfg.get("digikey_per_minute", 30)))
        self._client_lock = None
        self._client: httpx.AsyncClient | None = None

    def _endpoints(self) -> tuple[str, str]:
        if get_settings().digikey_sandbox:
            return _BASE_SANDBOX, _TOKEN_URL_SANDBOX
        return _BASE_PROD, _TOKEN_URL_PROD

    @property
    def available(self) -> bool:
        s = get_settings()
        return bool(s.digikey_client_id and s.digikey_client_secret)

    async def search(self, mpn: str, manufacturer: str, region: str, currency: str) -> SourceResult:
        if not self.available:
            return SourceResult(
                source_name=self.name,
                source_type=self.source_type,
                source_url=_BASE_PROD,
                retrieved_at=datetime.now(UTC),
                region=region,
                currency=currency,
                confidence=0.0,
                errors=["DigiKey credentials not configured — using demo data instead."],
            )
        base, token_url = self._endpoints()
        self._headers = {
            **_HEADERS,
            "X-DIGIKEY-Locale-Site": region,
            "X-DIGIKEY-Locale-Currency": currency,
        }
        await self._bucket.acquire()
        timeout = config_section("orchestrator").get("timeout_seconds", 25)
        async with httpx.AsyncClient(timeout=timeout) as client:
            token, tok_error = await self._ensure_token(client, token_url)
            if token is None:
                return SourceResult(
                    source_name=self.name,
                    source_type=self.source_type,
                    source_url=token_url,
                    retrieved_at=datetime.now(UTC),
                    region=region,
                    currency=currency,
                    confidence=0.0,
                    errors=[tok_error or "DigiKey OAuth2 token request failed."],
                )
            headers = {
                **self._headers,
                "X-DIGIKEY-Client-Id": get_settings().digikey_client_id,
                "Authorization": f"Bearer {token}",
            }
            resp = await client.post(
                f"{base}/products/v4/search/keyword",
                json={"Keywords": mpn, "Limit": 10, "Offset": 0},
                headers=headers,
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
                    errors=[f"DigiKey HTTP {resp.status_code}: {resp.text[:300]}"],
                )
            body = resp.json()
            results = body.get("Products") or body.get("results") or []
            exact = next(
                (p for p in results if (p.get("ManufacturerProductNumber") or "").upper() == mpn.upper()),
                None,
            )
            product = exact or (results[0] if results else None)
            if product is None:
                return SourceResult(
                    source_name=self.name,
                    source_type=self.source_type,
                    source_url=str(resp.url),
                    retrieved_at=datetime.now(UTC),
                    region=region,
                    currency=currency,
                    confidence=0.0,
                    errors=[f"DigiKey: no results for {mpn}."],
                )
            return self._to_result(product, str(resp.url), region, currency)

    async def _ensure_token(self, client: httpx.AsyncClient, token_url: str) -> tuple[str | None, str]:
        global _token, _token_expires
        if _token and _time.monotonic() < _token_expires - 60:
            return _token, ""
        s = get_settings()
        resp = await client.post(
            token_url,
            data={
                "client_id": s.digikey_client_id,
                "client_secret": s.digikey_client_secret,
                "grant_type": "client_credentials",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            return None, f"DigiKey OAuth2 token request failed (HTTP {resp.status_code}): {resp.text[:300]}"
        data = resp.json()
        _token = data.get("access_token", "")
        _token_expires = _time.monotonic() + float(data.get("expires_in", 3600))
        return (_token or None), ""

    def _to_result(self, p: dict[str, Any], url: str, region: str, currency: str) -> SourceResult:
        status_raw = str((p.get("ProductStatus") or {}).get("Status", "")).strip().lower()
        status = LifecycleStatus.UNKNOWN.value
        for key, mapped in _STATUS_MAP.items():
            if key in status_raw:
                status = mapped
                break
        params = {
            (pp.get("ParameterText") or "").lower(): str(pp.get("ValueText") or "")
            for pp in p.get("Parameters") or []
            if pp.get("ParameterText")
        }
        mfr = (p.get("Manufacturer") or {}).get("Name", "")
        mfr_mpn = p.get("ManufacturerProductNumber") or ""
        unit_price = p.get("UnitPrice")
        offers_breaks = (
            [PriceBreak(qty=1, price=float(unit_price), currency=currency)]
            if parse_float(unit_price) is not None
            else []
        )
        offer = Offer(
            distributor=self.name,
            available=bool(p.get("QuantityAvailable") or 0) or bool(offers_breaks),
            available_qty=parse_int(p.get("QuantityAvailable")),
            price_breaks=offers_breaks,
            moq=1 if offers_breaks else None,
            lead_time=_lead_weeks(p.get("ManufacturerLeadWeeks")) or str(p.get("LeadTime") or ""),
            currency=currency,
            region=region,
            url=p.get("ProductUrl") or "",
        )
        desc = p.get("Description") or {}
        series = p.get("Series") or {}
        desc_text = (
            desc.get("ProductDescription", "") if isinstance(desc, dict)
            else str(p.get("Description") or "")
        )
        facts = PartFacts(
            manufacturer=mfr,
            manufacturer_mpn=mfr_mpn,
            description=desc_text,
            category=(p.get("Category") or {}).get("Name", ""),
            family=series.get("Name", "") if isinstance(series, dict) else str(p.get("Series") or ""),
            package=_pick(params, "package / case", "supplier device package"),
            lifecycle_status=status,
            datasheet_url=p.get("DatasheetUrl") or "",
            product_url=p.get("ProductUrl") or "",
            rohs=_norm_rohs(_find_rohs(params)),
            operating_voltage=_pick(params, "voltage - supply", "output voltage"),
            current=_pick(params, "current - supply", "output current", "current - output", "supply current"),
            frequency=_pick(params, "frequency", "operating frequency", "clock frequency"),
            temperature=_pick(params, "operating temperature"),
        )
        return SourceResult(
            source_name=self.name,
            source_type=self.source_type,
            source_url=url,
            retrieved_at=datetime.now(UTC),
            region=region,
            currency=currency,
            confidence=0.9,
            facts=facts,
            offers=[offer] if (offer.available_qty or offers_breaks) else [],
        )


def _pick(params: dict[str, str], *keys: str) -> str:
    for key in keys:
        if key in params and params[key]:
            return str(params[key]).strip()
    return ""


def _find_rohs(params: dict[str, str]) -> str:
    for key, value in params.items():
        if "rohs" in key and value and value != "" and value != "Unknown":
            return value
    return ""


def _lead_weeks(value) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):g} weeks"
    except (TypeError, ValueError):
        return str(value)


def _norm_rohs(value) -> str:
    if not value:
        return "Unknown"
    v = str(value).lower()
    if "compliant" in v:
        return "Yes"
    if "noncompliant" in v or "non-compliant" in v:
        return "No"
    return str(value)
