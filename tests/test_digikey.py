"""DigiKey adapter: OAuth token endpoint + search host switch for sandbox vs production."""

from __future__ import annotations

import pytest

from app.sources.digikey import DigiKeyAdapter, reset_token_cache


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | str = {}, url: str = "https://api.digikey.com") -> None:
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

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url: str, **kwargs):
        _FakeClient.calls.append({"url": url, **kwargs})
        if "/oauth2/token" in url:
            if _FakeClient.next_token_status != 200:
                return _FakeResponse(
                    _FakeClient.next_token_status,
                    '{"ErrorMessage":"Invalid clientId","ErrorDetails":"clientId invalid for requested resource"}',
                )
            return _FakeResponse(200, {"access_token": "tok", "expires_in": 3600})
        return _FakeResponse(
            200,
            {
                "Products": [
                    {
                        "ManufacturerProductNumber": "NE555P",
                        "Manufacturer": {"Id": 296, "Name": "Texas Instruments"},
                        "ProductStatus": {"Id": 0, "Status": "Active"},
                        "Description": {"ProductDescription": "IC OSC SINGLE TIMER 100KHZ 8-DIP"},
                        "QuantityAvailable": 5000,
                        "ManufacturerLeadWeeks": 4,
                        "UnitPrice": 0.59,
                        "ProductUrl": "https://www.digikey.com/en/products/detail/NE555P/277057",
                        "DatasheetUrl": "https://www.ti.com/lit/ds/symlink/na555.pdf",
                        "Series": {"Id": 818, "Name": "-"},
                        "Category": {"CategoryId": 32, "Name": "Integrated Circuits (ICs)"},
                        "Parameters": [
                            {"ParameterText": "Type", "ValueText": "555 Type, Timer/Oscillator (Single)"},
                            {"ParameterText": "Frequency", "ValueText": "100kHz"},
                            {"ParameterText": "Voltage - Supply", "ValueText": "4.5V ~ 16V"},
                            {"ParameterText": "Current - Supply", "ValueText": "10 mA"},
                            {"ParameterText": "Operating Temperature", "ValueText": "0C ~ 70C"},
                            {"ParameterText": "Package / Case", "ValueText": "8-DIP (0.300\", 7.62mm)"},
                        ],
                    }
                ]
            },
        )


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("MOUSER_API_KEY", "")
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "cid")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "csecret")
    monkeypatch.delenv("DIGIKEY_SANDBOX", raising=False)
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    _FakeClient.calls.clear()
    _FakeClient.next_token_status = 200
    get_settings.cache_clear()
    reset_token_cache()
    yield
    get_settings.cache_clear()
    reset_token_cache()


async def test_production_uses_api_dot_digikey_dot_com():
    adapter = DigiKeyAdapter()
    result = await adapter.search("NE555P", "", "US", "USD")
    token_call = _FakeClient.calls[0]
    assert "https://api.digikey.com/v1/oauth2/token" in token_call["url"]
    search_call = next(c for c in _FakeClient.calls if "/search/keyword" in c["url"])
    assert "https://api.digikey.com/products/v4" in search_call["url"]
    assert result.facts is not None
    assert result.facts.manufacturer_mpn == "NE555P"


async def test_sandbox_uses_sandbox_api_host(monkeypatch):
    monkeypatch.setenv("DIGIKEY_SANDBOX", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    adapter = DigiKeyAdapter()
    result = await adapter.search("NE555P", "", "US", "USD")
    token_call = _FakeClient.calls[0]
    assert "https://sandbox-api.digikey.com/v1/oauth2/token" in token_call["url"]
    search_call = next(c for c in _FakeClient.calls if "/search/keyword" in c["url"])
    assert "https://sandbox-api.digikey.com/products/v4" in search_call["url"]
    assert result.facts is not None


async def test_token_rejection_surfaces_server_error():
    _FakeClient.next_token_status = 401
    adapter = DigiKeyAdapter()
    result = await adapter.search("NE555P", "", "US", "USD")
    assert result.confidence == 0.0
    assert result.errors and "HTTP 401" in result.errors[0]
    assert "Invalid clientId" in result.errors[0]


async def test_v4_response_maps_spec_fields():
    adapter = DigiKeyAdapter()
    result = await adapter.search("NE555P", "", "US", "USD")
    f = result.facts
    assert f is not None
    assert f.manufacturer == "Texas Instruments"
    assert f.manufacturer_mpn == "NE555P"
    assert f.description == "IC OSC SINGLE TIMER 100KHZ 8-DIP"
    assert f.category == "Integrated Circuits (ICs)"
    assert f.package == '8-DIP (0.300", 7.62mm)'
    assert f.datasheet_url == "https://www.ti.com/lit/ds/symlink/na555.pdf"
    assert f.product_url == "https://www.digikey.com/en/products/detail/NE555P/277057"
    assert f.operating_voltage == "4.5V ~ 16V"
    assert f.current == "10 mA"
    assert f.frequency == "100kHz"
    assert f.temperature == "0C ~ 70C"
    assert result.offers and result.offers[0].available_qty == 5000
    assert result.offers[0].price_breaks and result.offers[0].price_breaks[0].price == 0.59
    assert result.offers[0].lead_time == "4 weeks"
