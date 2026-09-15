"""Nexar adapter: OAuth2 client_credentials → GraphQL supSearchMpn."""

from __future__ import annotations

import pytest

from app.sources.nexar import NexarAdapter, reset_token_cache


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | str = {}, url: str = "https://api.nexar.com/graphql") -> None:
        self.status_code = status_code
        self._payload = payload
        self.url = url

    @property
    def text(self) -> str:
        if isinstance(self._payload, str):
            return self._payload
        import json

        return json.dumps(self._payload)

    def json(self):
        import json

        if isinstance(self._payload, str):
            return json.loads(self._payload)
        return self._payload


class _FakeClient:
    calls: list[dict] = []
    next_token_status = 200
    next_graphql_errors: list[dict] | None = None

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url: str, **kwargs):
        _FakeClient.calls.append({"url": url, **kwargs})

        if "identity.nexar.com" in url:
            if _FakeClient.next_token_status != 200:
                return _FakeResponse(_FakeClient.next_token_status, "Unauthorized", url=url)
            return _FakeResponse(200, {"access_token": "nx_token", "expires_in": 3600}, url=url)

        # GraphQL endpoint
        errors = _FakeClient.next_graphql_errors
        if errors is not None:
            return _FakeResponse(200, {"errors": errors, "data": None}, url=url)

        variables = (kwargs.get("json") or {}).get("variables") or {}
        q = variables.get("q", "")
        if q.upper().startswith("NOPART"):
            return _FakeResponse(
                200,
                {"data": {"supSearchMpn": {"hits": 0, "results": []}}},
                url=url,
            )

        return _FakeResponse(
            200,
            {
                "data": {
                    "supSearchMpn": {
                        "hits": 1,
                        "results": [
                            {
                                "description": (
                                    "Timer IC, Single, 100 kHz, CMOS/TTL, 4.5V-16V, PDIP-8, Texas Instruments"
                                ),
                                "part": {
                                    "mpn": "NE555P",
                                    "shortDescription": "IC OSC SINGLE TIMER 100KHZ 8-DIP",
                                    "octopartUrl": "https://octopart.com/ne555p-texas+instruments-476",
                                    "totalAvail": 12500,
                                    "estimatedFactoryLeadDays": 14,
                                    "medianPrice1000": {"quantity": 1000, "price": 0.47, "currency": "USD"},
                                    "category": {"name": "Integrated Circuits (ICs)"},
                                    "manufacturer": {"name": "Texas Instruments"},
                                    "sellers": [
                                        {
                                            "company": {"name": "DigiKey"},
                                            "offers": [
                                                {
                                                    "inventoryLevel": 12500,
                                                    "moq": 1,
                                                    "factoryLeadDays": 8,
                                                    "clickUrl": "https://www.digikey.com/en/products/detail/NE555P/277057",
                                                    "prices": [
                                                        {"quantity": 1, "price": 0.73, "currency": "USD"},
                                                        {"quantity": 10, "price": 0.61, "currency": "USD"},
                                                        {"quantity": 100, "price": 0.55, "currency": "USD"},
                                                    ],
                                                }
                                            ],
                                        },
                                        {
                                            "company": {"name": "DigiKey"},
                                            "offers": [],
                                        },
                                        {
                                            "company": {"name": "Mouser"},
                                            "offers": [
                                                {
                                                    "inventoryLevel": 8000,
                                                    "moq": 1,
                                                    "factoryLeadDays": 10,
                                                    "clickUrl": "https://www.mouser.com/ProductDetail/Texas-Instruments/NE555P",
                                                    "prices": [
                                                        {"quantity": 1, "price": 0.71, "currency": "USD"},
                                                        {"quantity": 25, "price": 0.58, "currency": "USD"},
                                                    ],
                                                }
                                            ],
                                        },
                                    ],
                                },
                            }
                        ],
                    }
                }
            },
            url=url,
        )


def _patch(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("MOUSER_API_KEY", "")
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "")
    monkeypatch.setenv("NEXAR_CLIENT_ID", "test_id")
    monkeypatch.setenv("NEXAR_CLIENT_SECRET", "test_secret")
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    _FakeClient.calls.clear()
    _FakeClient.next_token_status = 200
    _FakeClient.next_graphql_errors = None
    get_settings.cache_clear()
    reset_token_cache()
    return get_settings


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    get_settings = _patch(monkeypatch)
    yield
    get_settings.cache_clear()
    reset_token_cache()


async def test_adapter_hits_identity_token_and_graphql():
    adapter = NexarAdapter()
    result = await adapter.search("NE555P", "Texas Instruments", "US", "USD")
    token_call = _FakeClient.calls[0]
    assert "identity.nexar.com/connect/token" in token_call["url"]
    graphql_call = _FakeClient.calls[1]
    assert "api.nexar.com/graphql" in graphql_call["url"]
    assert result.facts is not None
    assert result.facts.manufacturer_mpn == "NE555P"


async def test_exact_mpn_match_preferred():
    adapter = NexarAdapter()
    result = await adapter.search("NE555P", "", "US", "USD")
    assert result.facts is not None
    assert result.facts.manufacturer_mpn == "NE555P"


async def test_token_rejection_surfaces_error():
    _FakeClient.next_token_status = 401
    adapter = NexarAdapter()
    result = await adapter.search("NE555P", "", "US", "USD")
    assert result.confidence == 0.0
    assert result.errors and "HTTP 401" in result.errors[0]


async def test_graphql_error_surfaces_message():
    _FakeClient.next_graphql_errors = [{"message": "Cannot query field 'foo' on type 'SupPart'."}]
    adapter = NexarAdapter()
    result = await adapter.search("NE555P", "", "US", "USD")
    assert result.confidence == 0.0
    assert result.errors and "Cannot query field" in result.errors[0]


async def test_deduplicates_sellers_with_same_name():
    adapter = NexarAdapter()
    result = await adapter.search("NE555P", "", "US", "USD")
    names = [o.distributor for o in result.offers]
    assert len(names) == len(set(names))


async def test_facts_and_offers_map_correctly():
    adapter = NexarAdapter()
    result = await adapter.search("NE555P", "", "US", "USD")
    f = result.facts
    assert f is not None
    assert f.manufacturer == "Texas Instruments"
    assert f.description == "IC OSC SINGLE TIMER 100KHZ 8-DIP"
    assert f.category == "Integrated Circuits (ICs)"
    assert f.product_url == "https://octopart.com/ne555p-texas+instruments-476"
    assert result.offers and result.offers[0].available_qty == 12500
    assert result.offers[0].price_breaks
    assert result.offers[0].price_breaks[0].price == 0.73
    assert result.offers[0].moq == 1
    assert result.offers[0].lead_time == "8 days"


async def test_available_false_when_no_offers():
    _FakeClient.next_graphql_errors = None
    adapter = NexarAdapter()
    result = await adapter.search("NOPART123", "", "US", "USD")
    assert result.confidence == 0.0
    assert result.errors and "no results" in result.errors[0].lower()


async def test_no_credentials_returns_unavailable():
    import os

    from app.config import get_settings

    os.environ.pop("NEXAR_CLIENT_ID", None)
    os.environ.pop("NEXAR_CLIENT_SECRET", None)
    get_settings.cache_clear()
    adapter = NexarAdapter()
    assert adapter.available is False
    result = await adapter.search("NE555P", "", "US", "USD")
    assert result.confidence == 0.0
    assert result.errors and "credentials not configured" in result.errors[0].lower()


async def test_token_cache_reused(monkeypatch):
    _patch(monkeypatch)
    adapter = NexarAdapter()
    await adapter.search("NE555P", "", "US", "USD")
    first_token_call_count = sum(1 for c in _FakeClient.calls if "identity.nexar.com" in c["url"])
    assert first_token_call_count == 1

    await adapter.search("LM358", "", "US", "USD")
    second_token_call_count = sum(1 for c in _FakeClient.calls if "identity.nexar.com" in c["url"])
    assert second_token_call_count == 1
