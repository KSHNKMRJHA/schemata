"""Mouser adapter: endpoint selection and spec-attribute mapping."""

from __future__ import annotations

import pytest

from app.sources.mouser import MouserAdapter

_FAKE_PART = {
    "Manufacturer": "Texas Instruments",
    "ManufacturerPartNumber": "NE555P",
    "Description": "Precision Timer",
    "AvailabilityInStock": "8,842",
    "LifecycleStatus": "Active",
    "ROHSStatus": "RoHS Compliant",
    "ProductDetailUrl": "https://www.mouser.in/ProductDetail/595-NE555P",
    "DataSheetUrl": "https://www.ti.com/lit/ds/symlink/ne555.pdf",
    "ProductAttributes": [
        {"AttributeName": "Package", "AttributeValue": "PDIP-8"},
        {"AttributeName": "Output Current", "AttributeValue": "200 mA"},
        {"AttributeName": "Operating Temperature", "AttributeValue": "0 to 70 C"},
    ],
    "PriceBreaks": [{"Quantity": 1, "Price": "$0.20"}],
}


class _FakeResponse:
    status_code = 200

    def __init__(self, parts: list[dict], url: str = "https://api.mouser.com/search") -> None:
        self._parts = parts
        self.url = url

    @property
    def text(self) -> str:
        return "{}"

    def json(self):
        return {"SearchResults": {"Parts": self._parts}}


class _FakeClient:
    captured: tuple[str, dict] | None = None

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url: str, *, json: dict, headers: dict):
        _FakeClient.captured = (url, json)
        return _FakeResponse([_FAKE_PART])


@pytest.fixture(autouse=True)
def _fake_http(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("MOUSER_API_KEY", "live-key")
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_no_manufacturer_uses_partnumber_endpoint():
    adapter = MouserAdapter()
    result = await adapter.search("NE555P", "", "US", "USD")
    assert _FakeClient.captured is not None
    url, payload = _FakeClient.captured
    assert "api/v2/search/partnumber?" in url
    assert "SearchByPartRequest" in payload
    assert "SearchByPartMfrNameRequest" not in payload
    assert result.facts is not None
    assert result.facts.manufacturer_mpn == "NE555P"


async def test_with_manufacturer_uses_partnumberandmanufacturer_endpoint():
    adapter = MouserAdapter()
    result = await adapter.search("NE555P", "Texas Instruments", "US", "USD")
    url, payload = _FakeClient.captured
    assert "api/v2/search/partnumberandmanufacturer?" in url
    assert "SearchByPartMfrNameRequest" in payload
    assert result.facts is not None


async def test_maps_rohs_reach_datasheet_and_product_page():
    adapter = MouserAdapter()
    result = await adapter.search("NE555P", "", "US", "USD")
    f = result.facts
    assert f.rohs == "RoHS Compliant"
    assert f.datasheet_url == "https://www.ti.com/lit/ds/symlink/ne555.pdf"
    assert f.product_url == "https://www.mouser.in/ProductDetail/595-NE555P"


async def test_maps_productattributes_into_spec_columns():
    adapter = MouserAdapter()
    result = await adapter.search("NE555P", "", "US", "USD")
    f = result.facts
    assert f.package == "PDIP-8"
    assert f.current == "200 mA"
    assert f.temperature == "0 to 70 C"
