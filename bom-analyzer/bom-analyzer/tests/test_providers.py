"""
Provider tests.

The live providers are exercised against recorded response payloads rather
than the network: a fake transport is substituted for the HTTP client, so the
parsing, credential handling, error handling and rate-limit behaviour are all
covered without a single outbound request or an API key.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bomiq.config import Config  # noqa: E402
from bomiq.core.errors import ProviderError  # noqa: E402
from bomiq.core.models import ComplianceState, Lifecycle  # noqa: E402
from bomiq.providers.digikey import DigiKeyProvider  # noqa: E402
from bomiq.providers.farnell import FarnellProvider  # noqa: E402
from bomiq.providers.mock import MockProvider  # noqa: E402
from bomiq.providers.mouser import MouserProvider  # noqa: E402
from bomiq.providers.nexar import NexarProvider  # noqa: E402
from bomiq.providers.registry import ProviderRegistry  # noqa: E402
from bomiq.util.http import (  # noqa: E402
    CircuitBreaker, HttpError, Response, TokenBucket,
)


def temp_config(**settings) -> Config:
    directory = Path(tempfile.mkdtemp(prefix="bomiq-prov-"))
    config = Config(data_dir=directory / "data", config_dir=directory / "cfg",
                    load_env=False, use_keyring=False)
    config.settings.update(settings)
    return config


class FakeHttp:
    """Stands in for ``HttpClient``; returns canned responses by URL match."""

    def __init__(self, routes: dict[str, object], default=None):
        self.routes = routes
        self.default = default
        self.calls: list[tuple[str, str]] = []
        self.stats = {"requests": 0}

    def _match(self, url: str):
        for fragment, payload in self.routes.items():
            if fragment in url:
                return payload
        if self.default is not None:
            return self.default
        raise HttpError(f"No fake route for {url}", status=404, url=url)

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url))
        self.stats["requests"] += 1
        payload = self._match(url)
        if isinstance(payload, Exception):
            raise payload
        if callable(payload):
            payload = payload(url, kwargs)
        body = json.dumps(payload).encode() if not isinstance(payload, bytes) \
            else payload
        return Response(status=200, headers={"content-type": "application/json"},
                        body=body, url=url)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def set_rate(self, *args, **kwargs):
        pass

    def host_health(self):
        return {}


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #

class TestHttpPlumbing(unittest.TestCase):
    def test_token_bucket_limits_rate(self):
        bucket = TokenBucket(rate=20.0, burst=1)
        started = time.monotonic()
        for _ in range(4):
            self.assertTrue(bucket.acquire(timeout=5))
        elapsed = time.monotonic() - started
        self.assertGreater(elapsed, 0.1)

    def test_token_bucket_is_thread_safe(self):
        bucket = TokenBucket(rate=500.0, burst=5)
        results: list[bool] = []
        lock = threading.Lock()

        def worker():
            ok = bucket.acquire(timeout=5)
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 20)
        self.assertTrue(all(results))

    def test_circuit_breaker_opens_and_recovers(self):
        breaker = CircuitBreaker(threshold=3, cooldown=0.2)
        for _ in range(3):
            breaker.record_failure()
        self.assertTrue(breaker.is_open)
        self.assertFalse(breaker.allow())
        time.sleep(0.25)
        self.assertTrue(breaker.allow())
        breaker.record_success()
        self.assertFalse(breaker.is_open)


# --------------------------------------------------------------------------- #
# Offline provider
# --------------------------------------------------------------------------- #

class TestMockProvider(unittest.TestCase):
    def setUp(self):
        self.provider = MockProvider(temp_config())

    def test_known_part(self):
        part = self.provider.lookup("LM358DR")
        self.assertIsNotNone(part)
        self.assertEqual(part.manufacturer, "Texas Instruments")
        self.assertTrue(part.offers)

    def test_deterministic(self):
        a = self.provider.lookup("GRM188R71C104KA01D", use_cache=False)
        b = self.provider.lookup("GRM188R71C104KA01D", use_cache=False)
        self.assertEqual(a.total_stock, b.total_stock)
        self.assertEqual(a.lifecycle, b.lifecycle)

    def test_case_and_punctuation_insensitive(self):
        a = self.provider.lookup("lm358dr", use_cache=False)
        self.assertEqual(a.mpn, "LM358DR")

    def test_junk_is_not_found(self):
        self.assertIsNone(self.provider.lookup("??"))
        self.assertIsNone(self.provider.lookup("ABCDE"))

    def test_unknown_part_is_marked_synthesised(self):
        part = self.provider.lookup("ZZZZ99999-XYZ", use_cache=False)
        self.assertIsNotNone(part)
        self.assertIn("Synthesised", part.specs.get("Record type", ""))
        self.assertEqual(part.lifecycle, Lifecycle.UNKNOWN)

    def test_every_offer_is_labelled_synthetic(self):
        part = self.provider.lookup("LM358DR", use_cache=False)
        for offer in part.offers:
            self.assertTrue(offer.warnings)

    def test_known_obsolete_part(self):
        part = self.provider.lookup("MPU-6050", use_cache=False)
        self.assertEqual(part.lifecycle, Lifecycle.OBSOLETE)
        self.assertEqual(part.total_stock, 0)

    def test_alternate_groups(self):
        part = self.provider.lookup("RC0603FR-0710KL", use_cache=False)
        self.assertIn("CRCW060310K0FKEA", part.alternate_mpns)

    def test_search(self):
        parts = self.provider.search("CAP CER 0603", limit=5)
        self.assertTrue(parts)

    def test_no_credentials_needed(self):
        ok, _ = self.provider.check_credentials()
        self.assertTrue(ok)

    def test_never_touches_the_network(self):
        self.assertIsNone(self.provider.part_cache)
        # A provider with no HTTP routes configured would raise if it tried.
        self.provider.http = FakeHttp({}, default=None)
        self.assertIsNotNone(self.provider.lookup("LM358DR", use_cache=False))


# --------------------------------------------------------------------------- #
# DigiKey
# --------------------------------------------------------------------------- #

DIGIKEY_PRODUCT = {
    "Product": {
        "ManufacturerProductNumber": "RC0603FR-0710KL",
        "Manufacturer": {"Name": "YAGEO"},
        "Description": {
            "ProductDescription": "RES SMD 10K OHM 1% 1/10W 0603",
            "DetailedDescription": "10 kOhms +/-1% 0.1W 0603",
        },
        "Category": {"Name": "Resistors"},
        "Series": {"Name": "RC"},
        "ProductStatus": {"Status": "Active"},
        "DatasheetUrl": "https://example.com/ds.pdf",
        "PhotoUrl": "https://example.com/p.jpg",
        "ProductUrl": "https://www.digikey.com/x",
        "QuantityAvailable": 621642,
        "ManufacturerLeadWeeks": "12 weeks",
        "UnitPrice": 0.0032,
        "Parameters": [
            {"ParameterText": "Resistance", "ValueText": "10 kOhms"},
            {"ParameterText": "Tolerance", "ValueText": "±1%"},
            {"ParameterText": "Package / Case", "ValueText": "0603 (1608 Metric)"},
        ],
        "Classifications": {
            "RohsStatus": "ROHS3 Compliant",
            "ReachStatus": "REACH Unaffected",
            "MoistureSensitivityLevel": "1",
            "ExportControlClassNumber": "EAR99",
            "HtsusCode": "8533.21.0000",
        },
        "ProductVariations": [
            {
                "DigiKeyProductNumber": "311-10.0KHRCT-ND",
                "PackageType": {"Name": "Cut Tape (CT)"},
                "QuantityAvailableforPackageType": 621642,
                "MinimumOrderQuantity": 1,
                "StandardPackage": 1,
                "StandardPricing": [
                    {"BreakQuantity": 1, "UnitPrice": 0.1},
                    {"BreakQuantity": 100, "UnitPrice": 0.0182},
                    {"BreakQuantity": 1000, "UnitPrice": 0.0075},
                ],
            },
            {
                "DigiKeyProductNumber": "311-10.0KHRTR-ND",
                "PackageType": {"Name": "Tape & Reel (TR)"},
                "QuantityAvailableforPackageType": 500000,
                "MinimumOrderQuantity": 5000,
                "StandardPackage": 5000,
                "StandardPricing": [
                    {"BreakQuantity": 5000, "UnitPrice": 0.0038},
                ],
            },
        ],
        "OtherNames": ["RC0603FR-0710KL "],
    }
}


class TestDigiKey(unittest.TestCase):
    def provider(self, routes=None, **settings):
        config = temp_config(**settings)
        config.credentials.set("digikey", {"client_id": "id",
                                           "client_secret": "secret"})
        provider = DigiKeyProvider(config)
        provider.http = FakeHttp(routes or {
            "/v1/oauth2/token": {"access_token": "tok", "expires_in": 600},
            "/productdetails": DIGIKEY_PRODUCT,
        })
        return provider

    def test_parses_a_product(self):
        part = self.provider().lookup("RC0603FR-0710KL", use_cache=False)
        self.assertEqual(part.mpn, "RC0603FR-0710KL")
        self.assertEqual(part.manufacturer, "Yageo")
        self.assertEqual(part.lifecycle, Lifecycle.ACTIVE)
        self.assertEqual(part.package, "0603")
        self.assertEqual(part.total_avail, 621642)
        self.assertEqual(part.estimated_factory_lead_days, 84)
        self.assertEqual(len(part.offers), 2)

    def test_price_breaks_and_pack_sizes(self):
        part = self.provider().lookup("RC0603FR-0710KL", use_cache=False)
        reel = next(o for o in part.offers if "Reel" in o.packaging)
        self.assertEqual(reel.moq, 5000)
        self.assertEqual(reel.spq, 5000)
        cut = next(o for o in part.offers if "Cut" in o.packaging)
        self.assertEqual(len(cut.price_breaks), 3)
        self.assertEqual(cut.min_unit_price, Decimal("0.0075"))

    def test_compliance_extracted(self):
        part = self.provider().lookup("RC0603FR-0710KL", use_cache=False)
        self.assertEqual(part.compliance.rohs, ComplianceState.COMPLIANT)
        self.assertEqual(part.compliance.hts_code, "8533.21.0000")
        self.assertEqual(part.compliance.eccn, "EAR99")
        self.assertFalse(part.compliance.export_controlled)

    def test_token_is_cached(self):
        provider = self.provider()
        provider.lookup("RC0603FR-0710KL", use_cache=False)
        provider.lookup("LM358DR", use_cache=False)
        token_calls = [c for c in provider.http.calls if "token" in c[1]]
        self.assertEqual(len(token_calls), 1)

    def test_missing_credentials_raise_clearly(self):
        config = temp_config()
        provider = DigiKeyProvider(config)
        provider.http = FakeHttp({})
        with self.assertRaises(ProviderError) as context:
            provider.lookup("LM358DR", use_cache=False)
        self.assertIn("client id", str(context.exception).lower())

    def test_missing_token_is_an_actionable_error(self):
        provider = self.provider(routes={
            "/v1/oauth2/token": {"error": "invalid_client"},
        })
        with self.assertRaises(ProviderError) as context:
            provider.lookup("LM358DR", use_cache=False)
        self.assertIn("access token", str(context.exception))

    def test_auth_failure_disables_the_provider(self):
        provider = self.provider(routes={
            "/v1/oauth2/token": {"access_token": "tok", "expires_in": 600},
            "/productdetails": HttpError("forbidden", status=403),
        })
        with self.assertRaises(ProviderError):
            provider.lookup("LM358DR", use_cache=False)
        self.assertFalse(provider.available)
        self.assertIn("credentials", provider.disabled_reason)

    def test_sandbox_host_setting(self):
        provider = self.provider(digikey_sandbox=True)
        self.assertIn("sandbox", provider.host)


# --------------------------------------------------------------------------- #
# Mouser
# --------------------------------------------------------------------------- #

MOUSER_RESPONSE = {
    "Errors": [],
    "SearchResults": {
        "NumberOfResult": 1,
        "Parts": [{
            "ManufacturerPartNumber": "GRM188R71C104KA01D",
            "Manufacturer": "Murata Electronics",
            "Description": "Multilayer Ceramic Capacitors MLCC 0.1uF 16V X7R",
            "Category": "Ceramic Capacitors",
            "LifecycleStatus": "Not Recommended for New Designs",
            "DataSheetUrl": "https://example.com/m.pdf",
            "ProductDetailUrl": "https://www.mouser.com/x",
            "MouserPartNumber": "81-GRM188R71C104KA1D",
            "Availability": "321008 In Stock",
            "AvailabilityInStock": "321008",
            "FactoryStock": "1000000",
            "LeadTime": "14 Weeks",
            "Min": "1",
            "Mult": "1",
            "ROHSStatus": "RoHS Compliant",
            "PriceBreaks": [
                {"Quantity": 1, "Price": "0.10", "Currency": "USD"},
                {"Quantity": 100, "Price": "0.0182", "Currency": "USD"},
            ],
            "ProductAttributes": [
                {"AttributeName": "Capacitance", "AttributeValue": "0.1 uF"},
                {"AttributeName": "Package / Case", "AttributeValue": "0603"},
            ],
            "ProductCompliance": [
                {"ComplianceName": "USHTS", "ComplianceValue": "8532.24.0020"},
                {"ComplianceName": "ECCN", "ComplianceValue": "EAR99"},
                {"ComplianceName": "COO", "ComplianceValue": "JP"},
            ],
            "SuggestedReplacement": "GRM188R71C104KA57D",
        }],
    },
}


class TestMouser(unittest.TestCase):
    def provider(self, routes=None):
        config = temp_config()
        config.credentials.set("mouser", {"api_key": "key"})
        provider = MouserProvider(config)
        provider.http = FakeHttp(routes or {"/search/": MOUSER_RESPONSE})
        return provider

    def test_parses_a_part(self):
        part = self.provider().lookup("GRM188R71C104KA01D", use_cache=False)
        self.assertEqual(part.manufacturer, "Murata Electronics")
        self.assertEqual(part.lifecycle, Lifecycle.NRND)
        self.assertEqual(part.total_stock, 321008)
        self.assertEqual(part.offers[0].lead_time_days, 0)
        self.assertEqual(part.offers[0].factory_stock, 1000000)

    def test_compliance_block(self):
        part = self.provider().lookup("GRM188R71C104KA01D", use_cache=False)
        self.assertEqual(part.compliance.hts_code, "8532.24.0020")
        self.assertEqual(part.compliance.country_of_origin, "JP")
        self.assertEqual(part.compliance.rohs, ComplianceState.COMPLIANT)

    def test_suggested_replacement_becomes_an_alternate(self):
        part = self.provider().lookup("GRM188R71C104KA01D", use_cache=False)
        self.assertIn("GRM188R71C104KA57D", part.alternate_mpns)

    def test_invalid_key_reported_from_a_200_response(self):
        provider = self.provider(routes={"/search/": {
            "Errors": [{"Message": "Invalid unique identifier (API key)"}],
            "SearchResults": None,
        }})
        with self.assertRaises(ProviderError) as context:
            provider.lookup("X", use_cache=False)
        self.assertIn("rejected the API key", str(context.exception))

    def test_quota_error_is_retryable(self):
        provider = self.provider(routes={"/search/": {
            "Errors": [{"Message": "Daily limit exceeded"}],
            "SearchResults": None,
        }})
        with self.assertRaises(ProviderError) as context:
            provider.lookup("X", use_cache=False)
        self.assertTrue(context.exception.retryable)

    def test_empty_result_is_not_found_not_an_error(self):
        provider = self.provider(routes={"/search/": {
            "Errors": [], "SearchResults": {"NumberOfResult": 0, "Parts": []},
        }})
        self.assertIsNone(provider.lookup("NOSUCHPART", use_cache=False))


# --------------------------------------------------------------------------- #
# Nexar
# --------------------------------------------------------------------------- #

NEXAR_RESPONSE = {
    "data": {
        "supMultiMatch": [{
            "reference": "0",
            "hits": 1,
            "parts": [{
                "id": "1",
                "mpn": "LM358DR",
                "manufacturer": {"name": "Texas Instruments"},
                "shortDescription": "IC OPAMP GP 2 CIRCUIT SOIC-8",
                "octopartUrl": "https://octopart.com/x",
                "totalAvail": 1234567,
                "estimatedFactoryLeadDays": 56,
                "medianPrice1000": {"price": 0.1234, "currency": "USD"},
                "bestDatasheet": {"url": "https://example.com/n.pdf"},
                "bestImage": {"url": "https://example.com/n.jpg"},
                "category": {"name": "Operational Amplifiers"},
                "specs": [
                    {"attribute": {"name": "Lifecycle Status",
                                   "shortname": "lifecyclestatus"},
                     "displayValue": "Active"},
                    {"attribute": {"name": "Case/Package",
                                   "shortname": "case_package"},
                     "displayValue": "SOIC-8"},
                    {"attribute": {"name": "RoHS",
                                   "shortname": "rohsstatus"},
                     "displayValue": "Compliant"},
                ],
                "similarParts": [{"mpn": "MC1458DR2G",
                                  "manufacturer": {"name": "ON Semiconductor"}}],
                "sellers": [
                    {
                        "company": {"name": "DigiKey"},
                        "isAuthorized": True,
                        "offers": [{
                            "sku": "296-1013-1-ND",
                            "inventoryLevel": 500000,
                            "moq": 1,
                            "orderMultiple": 1,
                            "packaging": "Cut Tape",
                            "clickUrl": "https://digikey.com/x",
                            "factoryLeadDays": 56,
                            "prices": [
                                {"quantity": 1, "price": 0.29,
                                 "currency": "USD"},
                                {"quantity": 1000, "price": 0.11,
                                 "currency": "USD"},
                            ],
                        }],
                    },
                    {
                        "company": {"name": "Broker Co"},
                        "isAuthorized": False,
                        "offers": [{
                            "sku": "BRK-1", "inventoryLevel": 100, "moq": 100,
                            "prices": [{"quantity": 100, "price": 1.50,
                                        "currency": "USD"}],
                        }],
                    },
                ],
            }],
        }]
    }
}


class TestNexar(unittest.TestCase):
    def provider(self, routes=None):
        config = temp_config()
        config.credentials.set("nexar", {"client_id": "id",
                                         "client_secret": "secret"})
        provider = NexarProvider(config)
        provider.http = FakeHttp(routes or {
            "identity.nexar.com": {"access_token": "tok", "expires_in": 86400},
            "api.nexar.com/graphql": NEXAR_RESPONSE,
        })
        return provider

    def test_parses_a_part(self):
        part = self.provider().lookup("LM358DR", use_cache=False)
        self.assertIsNotNone(part)
        self.assertEqual(part.manufacturer, "Texas Instruments")
        self.assertEqual(part.lifecycle, Lifecycle.ACTIVE)
        self.assertEqual(part.package, "SOIC-8")
        self.assertEqual(part.total_avail, 1234567)
        self.assertEqual(part.estimated_factory_lead_days, 56)
        self.assertEqual(part.median_price_1k, Decimal("0.1234"))

    def test_authorised_flag_is_preserved(self):
        part = self.provider().lookup("LM358DR", use_cache=False)
        authorised = [o for o in part.offers if o.authorized]
        brokers = [o for o in part.offers if not o.authorized]
        self.assertEqual(len(authorised), 1)
        self.assertEqual(len(brokers), 1)

    def test_similar_parts_captured(self):
        part = self.provider().lookup("LM358DR", use_cache=False)
        self.assertIn("MC1458DR2G", part.similar_mpns)

    def test_batch_lookup(self):
        provider = self.provider()
        results = provider.fetch_many([("LM358DR", "")])
        self.assertIn("LM358DR", results)

    def test_graphql_errors_are_reported(self):
        provider = self.provider(routes={
            "identity.nexar.com": {"access_token": "tok", "expires_in": 100},
            "api.nexar.com/graphql": {
                "errors": [{"message": "Cannot query field 'nope'"}]},
        })
        with self.assertRaises(ProviderError) as context:
            provider.lookup("LM358DR", use_cache=False)
        self.assertFalse(context.exception.retryable)

    def test_scope_error_is_not_retried(self):
        provider = self.provider(routes={
            "identity.nexar.com": {"access_token": "tok", "expires_in": 100},
            "api.nexar.com/graphql": {
                "errors": [{"message": "unauthorized: missing scope"}]},
        })
        with self.assertRaises(ProviderError) as context:
            provider.lookup("LM358DR", use_cache=False)
        self.assertFalse(context.exception.retryable)


# --------------------------------------------------------------------------- #
# Farnell
# --------------------------------------------------------------------------- #

FARNELL_RESPONSE = {
    "manufacturerPartNumberSearchReturn": {
        "numberOfResults": 1,
        "products": [{
            "sku": "2447145",
            "displayName": "RC0603FR-0710KL - SMD Chip Resistor",
            "translatedManufacturerPartNumber": "RC0603FR-0710KL",
            "brandName": "YAGEO",
            "productStatus": "ACTIVE",
            "rohsStatusCode": "1",
            "countryOfOrigin": "TW",
            "translatedMinimumOrderQuality": 50,
            "packSize": 50,
            "datasheets": [{"url": "https://example.com/f.pdf"}],
            "stock": {"level": 12500, "leastLeadTime": 42},
            "prices": [
                {"from": 50, "to": 499, "cost": 0.0104},
                {"from": 500, "to": 4999, "cost": 0.0071},
            ],
            "attributes": [
                {"attributeLabel": "Resistance", "attributeValue": "10",
                 "attributeUnit": "kohm"},
                {"attributeLabel": "Package / Case",
                 "attributeValue": "0603 [1608 Metric]"},
            ],
        }],
    }
}


class TestFarnell(unittest.TestCase):
    def provider(self, store="uk.farnell.com", routes=None):
        config = temp_config()
        config.credentials.set("farnell", {"api_key": "key", "store": store})
        provider = FarnellProvider(config)
        provider.http = FakeHttp(routes or {"catalog/products":
                                            FARNELL_RESPONSE})
        return provider

    def test_parses_a_part(self):
        part = self.provider().lookup("RC0603FR-0710KL", use_cache=False)
        self.assertEqual(part.mpn, "RC0603FR-0710KL")
        self.assertEqual(part.manufacturer, "Yageo")
        self.assertEqual(part.total_avail, 12500)
        self.assertEqual(part.offers[0].moq, 50)
        self.assertEqual(part.offers[0].currency, "GBP")

    def test_store_sets_the_currency(self):
        self.assertEqual(
            self.provider("in.element14.com").default_currency, "INR")
        self.assertEqual(
            self.provider("www.newark.com").default_currency, "USD")

    def test_price_ladder_uses_from_quantities(self):
        part = self.provider().lookup("RC0603FR-0710KL", use_cache=False)
        quantities = [b.quantity for b in part.offers[0].price_breaks]
        self.assertEqual(quantities, [50, 500])


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

class TestRegistry(unittest.TestCase):
    def test_falls_back_to_offline_without_keys(self):
        config = temp_config(providers=["digikey", "mouser"])
        registry = ProviderRegistry(config)
        self.assertEqual(registry.ids, ["mock"])
        self.assertTrue(registry.is_offline)
        self.assertIn("digikey", registry.setup_errors)

    def test_offline_setting_forces_mock(self):
        config = temp_config(offline=True)
        registry = ProviderRegistry(config)
        self.assertEqual(registry.ids, ["mock"])

    def test_lookup_many_deduplicates(self):
        config = temp_config(offline=True)
        registry = ProviderRegistry(config)
        results = registry.lookup_many([
            ("LM358DR", "TI"), ("lm358dr", ""), ("BAT54S", ""),
        ])
        self.assertEqual(len(results), 2)

    def test_progress_callback_is_called(self):
        config = temp_config(offline=True)
        registry = ProviderRegistry(config)
        seen: list[int] = []
        registry.lookup_many([("LM358DR", ""), ("BAT54S", "")],
                             progress=lambda done, total, mpn: seen.append(done))
        self.assertEqual(sorted(seen), [1, 2])

    def test_cancellation_stops_early(self):
        config = temp_config(offline=True)
        registry = ProviderRegistry(config)
        results = registry.lookup_many(
            [("LM358DR", ""), ("BAT54S", "")],
            should_cancel=lambda: True)
        self.assertEqual(len(results), 2)   # entries exist, may be unfilled

    def test_merge_across_providers(self):
        """Two providers disagreeing on lifecycle: the worst must win."""
        from bomiq.providers.base import merge_parts

        config = temp_config(offline=True)
        registry = ProviderRegistry(config)
        part = registry.lookup("LM358DR").part
        self.assertIsNotNone(part)
        clone = merge_parts([part, part])
        self.assertEqual(clone.lifecycle, part.lifecycle)

    def test_stats_reported(self):
        config = temp_config(offline=True)
        registry = ProviderRegistry(config)
        registry.lookup("LM358DR")
        stats = registry.stats()
        self.assertIn("mock", stats)
        self.assertGreaterEqual(stats["mock"]["calls"], 1)

    def test_provider_error_does_not_break_the_batch(self):
        config = temp_config(offline=True)
        registry = ProviderRegistry(config)
        provider = registry.providers["mock"]

        def explode(*args, **kwargs):
            raise ProviderError("mock", "boom")

        provider.fetch = explode  # type: ignore[assignment]
        results = registry.lookup_many([("LM358DR", ""), ("BAT54S", "")])
        self.assertEqual(len(results), 2)
        for result in results.values():
            self.assertFalse(result.found)
            self.assertIn("mock", result.errors)


if __name__ == "__main__":
    unittest.main()
