"""
Octopart / Nexar provider -- Supply GraphQL API.

This is the highest-value provider for BOM risk work: one call returns
lifecycle status, aggregated availability across ~150 distributors, median
pricing, specs, similar parts and compliance attributes.

Authentication
--------------
OAuth2 client credentials against ``https://identity.nexar.com/connect/token``
with ``scope=supply.domain``. Tokens last ~24 h; cached in memory.

Endpoint
--------
``POST https://api.nexar.com/graphql``

Note on quotas: Nexar bills per part queried, so this provider batches up to
20 MPNs per request via ``supMultiMatch`` when the engine asks for a batch, and
caches aggressively.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Sequence

from ..core.errors import ProviderError
from ..core.models import ComplianceState, Lifecycle, PartData
from ..util import log
from ..util.http import HttpError
from ..util.money import normalize_currency, to_decimal
from ..util.text import clean, normalize_mpn
from ..util.units import parse_int
from .base import (
    Provider, build_price_breaks, normalize_compliance_state,
    normalize_lifecycle,
)

LOG = log.get("providers.nexar")

TOKEN_URL = "https://identity.nexar.com/connect/token"
GRAPHQL_URL = "https://api.nexar.com/graphql"

MULTI_MATCH_QUERY = """
query BomIqMultiMatch($queries: [SupPartMatchQuery!]!, $currency: String!) {
  supMultiMatch(queries: $queries, currency: $currency, limit: 3) {
    reference
    hits
    parts {
      id
      mpn
      manufacturer { name }
      shortDescription
      octopartUrl
      totalAvail
      estimatedFactoryLeadDays
      medianPrice1000 { price currency }
      bestDatasheet { url }
      bestImage { url }
      category { name }
      specs { attribute { name shortname } displayValue }
      similarParts { mpn manufacturer { name } }
      sellers(includeBrokers: false) {
        company { name }
        isAuthorized
        offers {
          sku
          inventoryLevel
          moq
          orderMultiple
          packaging
          clickUrl
          updated
          factoryLeadDays
          onOrderQuantity
          prices { quantity price currency }
        }
      }
    }
  }
}
""".strip()

SEARCH_QUERY = """
query BomIqSearch($q: String!, $limit: Int!, $currency: String!) {
  supSearch(q: $q, limit: $limit, currency: $currency) {
    hits
    results {
      part {
        id
        mpn
        manufacturer { name }
        shortDescription
        octopartUrl
        totalAvail
        estimatedFactoryLeadDays
        medianPrice1000 { price currency }
        bestDatasheet { url }
        category { name }
        specs { attribute { name shortname } displayValue }
        sellers(includeBrokers: false) {
          company { name }
          isAuthorized
          offers {
            sku inventoryLevel moq orderMultiple packaging clickUrl
            factoryLeadDays
            prices { quantity price currency }
          }
        }
      }
    }
  }
}
""".strip()

_LIFECYCLE_SPEC_NAMES = {
    "lifecyclestatus", "lifecycle_status", "lifecycle", "partstatus",
    "part_status", "productstatus", "status",
}
_ROHS_SPEC_NAMES = {"rohsstatus", "rohs", "rohs_status"}
_REACH_SPEC_NAMES = {"reachstatus", "reach", "reach_svhc", "svhc"}
_PACKAGE_SPEC_NAMES = {"case_package", "casepackage", "package", "packagecase",
                       "case", "mountingstyle"}


class NexarProvider(Provider):
    id = "nexar"
    default_currency = "USD"
    can_suggest_alternates = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._token = ""
        self._token_expiry = 0.0
        self.currency = normalize_currency(self.config.settings.currency, "USD")

    # -- auth ------------------------------------------------------------- #

    def _access_token(self) -> str:
        with self._auth_lock:
            if self._token and time.monotonic() < self._token_expiry:
                return self._token
            response = self.http.post(
                TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.credential("client_id"),
                    "client_secret": self.credential("client_secret"),
                    "scope": "supply.domain",
                },
                headers={"Accept": "application/json"},
                cache_ttl=0, retries=2,
            )
            payload = response.json() or {}
            token = clean(payload.get("access_token"))
            if not token:
                raise ProviderError(
                    self.id,
                    "Nexar did not return an access token. Check the Client ID "
                    "and Secret in the Nexar portal, and that the application "
                    "has the Supply scope enabled.",
                    retryable=False,
                )
            expires_in = parse_int(payload.get("expires_in"), 86_400) or 86_400
            self._token = token
            self._token_expiry = time.monotonic() + max(300, expires_in - 300)
            LOG.info("Nexar token acquired (valid %ss)", expires_in)
            return self._token

    def _graphql(self, query: str, variables: dict[str, Any],
                 cache_ttl: int | None = None) -> dict[str, Any]:
        response = self.http.post(
            GRAPHQL_URL,
            json_body={"query": query, "variables": variables},
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Accept": "application/json",
            },
            cache_ttl=cache_ttl if cache_ttl is not None
            else self.config.settings.http_cache_ttl,
        )
        payload = response.json() or {}
        errors = payload.get("errors")
        if errors:
            messages = "; ".join(
                clean(error.get("message")) for error in errors
                if isinstance(error, dict)
            ) or str(errors)[:300]
            lowered = messages.lower()
            fatal = any(word in lowered for word in
                        ("unauthorized", "forbidden", "invalid token",
                         "not authorized", "scope", "subscription",
                         "cannot query field", "unknown argument",
                         "unknown type"))
            raise ProviderError(self.id, f"Nexar GraphQL error: {messages}",
                                retryable=not fatal)
        return payload.get("data") or {}

    # -- lookup ----------------------------------------------------------- #

    def fetch(self, mpn: str, manufacturer: str = "") -> PartData | None:
        results = self.fetch_many([(mpn, manufacturer)])
        return results.get(normalize_mpn(mpn))

    def fetch_many(self, requests: Sequence[tuple[str, str]]
                   ) -> dict[str, PartData]:
        """Batch lookup -- the whole reason to prefer Nexar for large BOMs."""
        queries: list[dict[str, Any]] = []
        for index, (mpn, manufacturer) in enumerate(requests):
            term = clean(mpn)
            if not term:
                continue
            query: dict[str, Any] = {"mpn": term, "reference": str(index)}
            if clean(manufacturer):
                query["mpn"] = term
                query["manufacturer"] = clean(manufacturer)
            queries.append(query)
        if not queries:
            return {}

        data = self._graphql(MULTI_MATCH_QUERY, {
            "queries": queries, "currency": self.currency,
        })
        out: dict[str, PartData] = {}
        for match in data.get("supMultiMatch") or []:
            reference = clean(match.get("reference"))
            try:
                request_index = int(reference)
            except (TypeError, ValueError):
                request_index = -1
            requested_mpn = (
                clean(requests[request_index][0])
                if 0 <= request_index < len(requests) else ""
            )
            parts = match.get("parts") or []
            if not parts:
                continue
            best = self._pick_best(parts, requested_mpn)
            part = self._to_part(best)
            if part.mpn:
                out[normalize_mpn(requested_mpn or part.mpn)] = part
        return out

    @staticmethod
    def _pick_best(parts: Sequence[dict[str, Any]], wanted: str
                   ) -> dict[str, Any]:
        target = normalize_mpn(wanted)

        def score(entry: dict[str, Any]) -> tuple[int, int]:
            candidate = normalize_mpn(entry.get("mpn"))
            exact = 2 if candidate == target else (
                1 if target and target in candidate else 0)
            return exact, parse_int(entry.get("totalAvail"), 0) or 0

        return max(parts, key=score)

    # -- mapping ---------------------------------------------------------- #

    def _to_part(self, entry: dict[str, Any]) -> PartData:
        specs: dict[str, str] = {}
        spec_by_shortname: dict[str, str] = {}
        for spec in entry.get("specs") or []:
            attribute = spec.get("attribute") or {}
            name = clean(attribute.get("name"))
            shortname = clean(attribute.get("shortname")).lower()
            value = clean(spec.get("displayValue"))
            if name and value:
                specs[name] = value
            if shortname and value:
                spec_by_shortname[shortname] = value

        lifecycle_raw = ""
        for key in _LIFECYCLE_SPEC_NAMES:
            if key in spec_by_shortname:
                lifecycle_raw = spec_by_shortname[key]
                break
        if not lifecycle_raw:
            for name, value in specs.items():
                if "lifecycle" in name.lower() or name.lower() == "part status":
                    lifecycle_raw = value
                    break
        lifecycle, note = normalize_lifecycle(lifecycle_raw)

        median = entry.get("medianPrice1000") or {}
        part = PartData(
            mpn=clean(entry.get("mpn")),
            manufacturer=clean((entry.get("manufacturer") or {}).get("name")),
            description=clean(entry.get("shortDescription")),
            category=clean((entry.get("category") or {}).get("name")),
            lifecycle=lifecycle,
            lifecycle_note=clean(note) or clean(lifecycle_raw),
            datasheet_url=clean((entry.get("bestDatasheet") or {}).get("url")),
            image_url=clean((entry.get("bestImage") or {}).get("url")),
            product_url=clean(entry.get("octopartUrl")),
            specs=specs,
            providers=[self.id],
            total_avail=parse_int(entry.get("totalAvail")),
            estimated_factory_lead_days=parse_int(
                entry.get("estimatedFactoryLeadDays")),
            median_price_1k=to_decimal(median.get("price")),
            median_price_1k_currency=normalize_currency(
                median.get("currency") or self.currency, self.currency),
        )
        for key in _PACKAGE_SPEC_NAMES:
            if key in spec_by_shortname:
                part.package = spec_by_shortname[key]
                break

        compliance = self.compliance_from_specs(specs)
        for key in _ROHS_SPEC_NAMES:
            if key in spec_by_shortname:
                compliance.rohs = normalize_compliance_state(
                    spec_by_shortname[key])
                compliance.rohs_note = spec_by_shortname[key]
                break
        for key in _REACH_SPEC_NAMES:
            if key in spec_by_shortname:
                compliance.reach = normalize_compliance_state(
                    spec_by_shortname[key])
                compliance.reach_note = spec_by_shortname[key]
                break
        compliance.sources.append("Octopart / Nexar")
        part.compliance = compliance

        for seller in entry.get("sellers") or []:
            company = clean((seller.get("company") or {}).get("name"))
            authorized = bool(seller.get("isAuthorized"))
            for raw_offer in seller.get("offers") or []:
                prices = raw_offer.get("prices") or []
                currency = self.currency
                if prices:
                    currency = normalize_currency(
                        (prices[0] or {}).get("currency") or currency, currency)
                breaks = build_price_breaks(
                    prices, currency,
                    qty_keys=("quantity",), price_keys=("price",))
                stock = parse_int(raw_offer.get("inventoryLevel"))
                offer = self.make_offer(
                    distributor=company or "Unknown distributor",
                    sku=clean(raw_offer.get("sku")),
                    packaging=clean(raw_offer.get("packaging")),
                    stock=stock,
                    on_order=parse_int(raw_offer.get("onOrderQuantity")),
                    moq=parse_int(raw_offer.get("moq")),
                    spq=parse_int(raw_offer.get("orderMultiple")),
                    order_multiple=parse_int(raw_offer.get("orderMultiple")),
                    lead_time_days=0 if (stock or 0) > 0 else self.lead_days(
                        raw_offer.get("factoryLeadDays")),
                    price_breaks=breaks,
                    currency=currency,
                    url=clean(raw_offer.get("clickUrl")),
                    authorized=authorized,
                    last_updated=clean(raw_offer.get("updated")),
                )
                if offer.price_breaks or offer.stock:
                    part.offers.append(offer)

        for similar in entry.get("similarParts") or []:
            mpn = clean(similar.get("mpn"))
            if mpn and normalize_mpn(mpn) != normalize_mpn(part.mpn):
                part.similar_mpns.append(mpn)
        return part

    # -- search / alternates ---------------------------------------------- #

    def search(self, query: str, limit: int = 10) -> list[PartData]:
        data = self._graphql(SEARCH_QUERY, {
            "q": clean(query), "limit": max(1, min(limit, 20)),
            "currency": self.currency,
        })
        results = (data.get("supSearch") or {}).get("results") or []
        out: list[PartData] = []
        for result in results[:limit]:
            entry = result.get("part")
            if isinstance(entry, dict):
                out.append(self._to_part(entry))
        return out

    def alternates(self, part: PartData, limit: int = 10) -> list[PartData]:
        """Nexar's ``similarParts`` are already on the record; resolve them."""
        wanted = list(part.similar_mpns)[:limit]
        if not wanted:
            return []
        try:
            resolved = self.fetch_many([(mpn, "") for mpn in wanted])
        except (ProviderError, HttpError) as exc:
            LOG.info("Could not resolve Nexar alternates: %s", exc)
            return []
        return list(resolved.values())

    def test_mpn(self) -> str:
        return "LM358DR"
