"""
Regression tests.

One test per defect found in the correctness audit, each asserting the
*observable* wrong behaviour is gone rather than just that the code changed.
The comment on each test records what the bug did, so a future refactor that
reintroduces it fails here with an explanation.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bomiq.analysis import cost as cost_mod  # noqa: E402
from bomiq.analysis import health as health_mod  # noqa: E402
from bomiq.analysis import risk as risk_mod  # noqa: E402
from bomiq.config import Config, Settings  # noqa: E402
from bomiq.core.models import (  # noqa: E402
    Bom, BomLine, Compliance, Lifecycle, LineResult, Match, MatchKind, Offer,
    PartData, PriceBreak, RiskLevel,
)
from bomiq.engine import Engine  # noqa: E402
from bomiq.export import flat  # noqa: E402
from bomiq.export.xlsx_writer import Workbook  # noqa: E402
from bomiq.ingest.pipeline import IngestOptions, Ingestor  # noqa: E402
from bomiq.ingest.readers import read_ods, read_xlsx_stdlib  # noqa: E402
from bomiq.ingest.validate import validate  # noqa: E402
from bomiq.providers.base import build_price_breaks, merge_parts  # noqa: E402
from bomiq.util.http import HttpClient  # noqa: E402
from bomiq.util.money import FxTable  # noqa: E402
from bomiq.util.text import normalize_distributor  # noqa: E402
from bomiq.util.units import parse_lead_time_days, parse_quantity  # noqa: E402


def temp_config(**settings) -> Config:
    directory = Path(tempfile.mkdtemp(prefix="bomiq-reg-"))
    config = Config(data_dir=directory / "data", config_dir=directory / "cfg",
                    load_env=False, use_keyring=False)
    config.settings.offline = True
    config.settings.update(settings)
    return config


def ingest(text: str, name: str = "t.csv", encoding: str = "utf-8", **options):
    return Ingestor().ingest_bytes(
        text.encode(encoding), name=name,
        options=IngestOptions(learn_templates=False, **options))


def offer(distributor="DigiKey", provider="test", sku="SKU-1", stock=10_000,
          moq=1, spq=1, breaks=(("1", "1.00"),), currency="USD",
          authorized=True, lead=0):
    return Offer(
        provider=provider, distributor=distributor, sku=sku, stock=stock,
        moq=moq, spq=spq, currency=currency, authorized=authorized,
        lead_time_days=lead,
        price_breaks=[PriceBreak(quantity=int(q), unit_price=Decimal(p),
                                 currency=currency) for q, p in breaks],
    )


def part(mpn="LM358DR", **kwargs):
    defaults = dict(manufacturer="Texas Instruments", package="SOIC-8",
                    description="IC OPAMP GP 2 CIRCUIT SOIC-8",
                    lifecycle=Lifecycle.ACTIVE, providers=["test"],
                    offers=[offer()])
    defaults.update(kwargs)
    return PartData(mpn=mpn, **defaults)


# --------------------------------------------------------------------------- #

class TestBuildQuantityIsolation(unittest.TestCase):
    """A BOM carrying its own build quantity used to overwrite the shared
    Settings, so every later analysis in the session was costed at that
    quantity -- a 500x error on the next BOM."""

    def test_build_quantity_does_not_leak_between_runs(self):
        engine = Engine(temp_config(build_quantity=1))
        try:
            first = Bom(name="a", build_quantity=500)
            first.lines = [BomLine(line_no=1, mpn="LM358DR", quantity=2)]
            engine.analyse_bom(first)

            second = Bom(name="b", build_quantity=1)
            second.lines = [BomLine(line_no=1, mpn="LM358DR", quantity=2)]
            result = engine.analyse_bom(second)

            self.assertEqual(result.summary.build_quantity, 1)
            self.assertEqual(result.results[0].cost.required_qty, 2)
            self.assertEqual(engine.config.settings.build_quantity, 1)
        finally:
            engine.close()


class TestNoSilentRowLoss(unittest.TestCase):
    """A ``Remarks:`` or ``Notes:`` row inside the table truncated the BOM and
    deleted every part below it with no warning; a long blank gap did the
    same."""

    def test_notes_row_mid_table_keeps_all_lines(self):
        rows = ["MPN,Qty,Manufacturer"]
        rows += [f"PART-{i:04d},1,Acme" for i in range(20)]
        rows.insert(19, "Remarks: all parts RoHS")
        result = ingest("\n".join(rows) + "\n")
        self.assertEqual(result.bom.line_count, 20)
        self.assertEqual(result.bom.lines[-1].mpn, "PART-0019")

    def test_notes_row_second_to_last_keeps_the_last_part(self):
        rows = ["MPN,Qty,Manufacturer"]
        rows += [f"PART-{i:04d},1,Acme" for i in range(6)]
        rows.insert(-1, "Note: see drawing")
        result = ingest("\n".join(rows) + "\n")
        self.assertIn("PART-0005", [line.mpn for line in result.bom.lines])

    def test_long_blank_gap_does_not_discard_the_rest(self):
        rows = ["MPN,Qty,Manufacturer"]
        rows += [f"A-{i:04d},1,Acme" for i in range(10)]
        rows += [",,"] * 40
        rows += [f"B-{i:04d},1,Acme" for i in range(10)]
        result = ingest("\n".join(rows) + "\n")
        self.assertEqual(result.bom.line_count, 20)

    def test_trailing_total_row_is_still_excluded(self):
        rows = ["MPN,Qty"] + [f"P-{i},1" for i in range(5)] + ["TOTAL,5"]
        result = ingest("\n".join(rows) + "\n")
        self.assertEqual(result.bom.line_count, 5)


class TestXlsxReaderIntegrity(unittest.TestCase):
    """The stdlib reader collected cell text on the iterparse ``start`` event,
    so any value straddling a read-buffer boundary came back blank. It showed
    up as scattered empty cells in workbooks over ~100 kB."""

    def test_large_workbook_has_no_blank_cells(self):
        wb = Workbook()
        sheet = wb.sheet("Big")
        sheet.row(["MPN", "Qty", "Manufacturer", "Description", "Price"])
        for index in range(2500):
            sheet.row([f"PART-{index:06d}-XYZ", index % 7 + 1,
                       f"Manufacturer Number {index}",
                       f"A reasonably long description for part {index}",
                       round(0.001 * index, 5)])
        path = wb.save(Path(tempfile.mkdtemp()) / "big.xlsx")
        self.assertGreater(path.stat().st_size, 60_000)

        rows = read_xlsx_stdlib(path.read_bytes())[0].rows
        self.assertEqual(len(rows), 2501)
        blanks = [index for index, row in enumerate(rows)
                  if index and (not row[0] or not row[2] or not row[3])]
        self.assertEqual(blanks, [])
        self.assertEqual(rows[2500][0], "PART-002499-XYZ")


class TestOdsMergedCells(unittest.TestCase):
    """``covered-table-cell`` elements were skipped, so every cell to the right
    of a horizontally merged cell shifted one column left and landed in the
    wrong field."""

    CONTENT = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content'
        ' xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
        ' xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"'
        ' xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        '<office:body><office:spreadsheet>'
        '<table:table table:name="BOM"><table:table-row>'
        '<table:table-cell table:number-columns-spanned="2">'
        '<text:p>MERGED</text:p></table:table-cell>'
        '<table:covered-table-cell/>'
        '<table:table-cell><text:p>Third</text:p></table:table-cell>'
        '</table:table-row></table:table>'
        '</office:spreadsheet></office:body></office:document-content>'
    )

    def test_covered_cells_hold_their_column(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("mimetype",
                             "application/vnd.oasis.opendocument.spreadsheet")
            archive.writestr("content.xml", self.CONTENT)
        rows = read_ods(buffer.getvalue())[0].rows
        self.assertEqual(rows[0], ["MERGED", "", "Third"])


class TestSplitOrderPricing(unittest.TestCase):
    """The split basket priced every leg at the break for the *full* required
    quantity, so a 400-piece leg was quoted at the 500-piece price and the
    basket total was 26% low. It also quoted sources below their MOQ."""

    def setUp(self):
        self.settings = Settings()
        self.settings.build_quantity = 1000
        self.fx = FxTable()

    def test_each_leg_is_priced_at_its_own_quantity(self):
        subject = part(offers=[
            offer("Alpha", sku="A", stock=400,
                  breaks=(("1", "1.00"), ("500", "0.50"))),
            offer("Beta", sku="B", stock=800,
                  breaks=(("1", "1.10"), ("500", "0.60"))),
        ])
        cost = cost_mod.cost_line(subject, 1.0, self.settings, self.fx)
        legs = [option for option in cost.options
                if any("split order" in note for note in option.notes)]
        self.assertEqual(len(legs), 2)
        by_distributor = {leg.distributor: leg for leg in legs}
        # 400 pieces from Alpha sits on its 1-piece break, not the 500 break.
        self.assertEqual(by_distributor["Alpha"].unit_price, Decimal("1.000000"))
        self.assertEqual(by_distributor["Beta"].unit_price, Decimal("0.600000"))
        self.assertEqual(cost.extended, Decimal("760.00"))

    def test_a_source_below_its_moq_is_skipped_not_quoted(self):
        subject = part(offers=[
            offer("BigMoq", sku="A", stock=400, moq=1000, spq=1000,
                  breaks=(("1000", "0.20"),)),
            offer("Beta", sku="B", stock=600, breaks=(("1", "1.00"),)),
            offer("Gamma", sku="C", stock=600, breaks=(("1", "1.05"),)),
        ])
        cost = cost_mod.cost_line(subject, 1.0, self.settings, self.fx)
        legs = [option for option in cost.options
                if any("split order" in note for note in option.notes)]
        self.assertNotIn("BigMoq", {leg.distributor for leg in legs})


class TestAggregatePriceCurrency(unittest.TestCase):
    """``median_price_1k`` was always converted as if it were USD, but
    providers return it in whatever currency they were queried in -- a 152x
    error for a JPY figure."""

    def test_currency_travels_with_the_aggregate_price(self):
        settings = Settings()
        settings.currency = "JPY"
        settings.build_quantity = 1000
        subject = part(offers=[])
        subject.median_price_1k = Decimal("152")
        subject.median_price_1k_currency = "JPY"
        cost = cost_mod.cost_line(subject, 1.0, settings, FxTable())
        self.assertTrue(cost.estimated)
        self.assertEqual(cost.unit_price, Decimal("152.000000"))

    def test_merge_keeps_the_currency_with_the_cheapest_price(self):
        cheap = part(offers=[])
        cheap.median_price_1k = Decimal("0.10")
        cheap.median_price_1k_currency = "EUR"
        cheap.providers = ["a"]
        dear = part(offers=[])
        dear.median_price_1k = Decimal("5.00")
        dear.median_price_1k_currency = "USD"
        dear.providers = ["b"]
        merged = merge_parts([dear, cheap])
        self.assertEqual(merged.median_price_1k, Decimal("0.10"))
        self.assertEqual(merged.median_price_1k_currency, "EUR")


class TestZeroPriceRejected(unittest.TestCase):
    """A price of exactly 0 was accepted as real, so a "call for pricing" part
    cost 0.00 and the BOM reported 100% cost coverage."""

    def test_zero_price_breaks_are_dropped(self):
        breaks = build_price_breaks([
            {"BreakQuantity": 1, "UnitPrice": 0.0},
            {"BreakQuantity": 100, "UnitPrice": 0.0},
        ])
        self.assertEqual(breaks, [])

    def test_a_zero_priced_offer_is_not_costed(self):
        settings = Settings()
        settings.build_quantity = 100
        subject = part(offers=[offer(breaks=(("1", "0.00"),))])
        # build_price_breaks is what strips these in the provider path; here
        # the offer has an explicit zero ladder, which must not produce a
        # 0.00 total presented as real.
        cost = cost_mod.cost_line(subject, 1.0, settings, FxTable())
        self.assertTrue(cost.extended is None or cost.estimated
                        or cost.extended > 0)


class TestDistributorAliasing(unittest.TestCase):
    """An aggregator calls DigiKey "Digi-Key", so merging its record with the
    direct provider's counted the same 50,000 pieces twice and turned a
    single-sourced part into a comfortably multi-sourced one."""

    def test_names_canonicalise(self):
        self.assertEqual(normalize_distributor("Digi-Key"), "DigiKey")
        self.assertEqual(normalize_distributor("element14"), "Farnell")
        self.assertEqual(normalize_distributor("Mouser"), "Mouser Electronics")

    def test_stock_is_counted_once_per_distributor(self):
        direct = part(providers=["digikey"],
                      offers=[offer("DigiKey", provider="digikey",
                                    sku="296-1013-1-ND", stock=50_000)])
        via_aggregator = part(providers=["nexar"],
                              offers=[offer("Digi-Key", provider="nexar",
                                            sku="296-1013-ND", stock=50_000)])
        merged = merge_parts([direct, via_aggregator])
        self.assertEqual(merged.total_stock, 50_000)
        self.assertEqual(
            len({o.distributor for o in merged.in_stock_offers}), 1)

    def test_genuine_packaging_variants_are_still_summed(self):
        subject = part(providers=["digikey"], offers=[
            offer("DigiKey", provider="digikey", sku="CT", stock=1_000),
            offer("DigiKey", provider="digikey", sku="TR", stock=4_000),
        ])
        merged = merge_parts([subject])
        self.assertEqual(merged.total_stock, 5_000)


class TestQuantityLocale(unittest.TestCase):
    """``parse_quantity`` treated a period as a decimal point unconditionally,
    so a German export's ``1.000`` became 1 instead of 1000."""

    def test_european_thousands_in_a_quantity_column(self):
        self.assertEqual(parse_quantity("1.000", decimal_comma=True), 1000.0)
        self.assertEqual(parse_quantity("2.500", decimal_comma=True), 2500.0)
        self.assertEqual(parse_quantity("1,5", decimal_comma=True), 1.5)

    def test_us_column_is_unaffected(self):
        self.assertEqual(parse_quantity("1,200"), 1200.0)
        self.assertEqual(parse_quantity("1.5"), 1.5)

    def test_end_to_end_on_a_german_export(self):
        text = ("Herstellernummer;Menge;Preis (EUR)\n"
                "RC0603FR-0710KL;1.000;0,0032\n"
                "GRM188R71C104KA01D;2.500;0,0182\n")
        result = ingest(text, name="de.csv", encoding="cp1252")
        quantities = [line.quantity for line in result.bom.lines]
        self.assertEqual(quantities, [1000.0, 2500.0])
        prices = [line.unit_price_in for line in result.bom.lines]
        self.assertEqual(prices, [Decimal("0.0032"), Decimal("0.0182")])


class TestBrokerOnlyRaisesRisk(unittest.TestCase):
    """Stock available only from non-authorised brokers *reduced* the sourcing
    score by 10, so an unvetted grey-market-only part read as Low risk."""

    def test_broker_only_is_worse_than_authorised(self):
        settings = Settings()
        broker_only = part(offers=[offer("Broker Co", authorized=False,
                                         stock=5_000)])
        authorised = part(offers=[offer("DigiKey", authorized=True,
                                        stock=5_000)])
        broker_factor = risk_mod.sourcing_factor(broker_only, 1.0)
        good_factor = risk_mod.sourcing_factor(authorised, 1.0)
        self.assertGreater(broker_factor.score, good_factor.score)
        self.assertGreater(broker_factor.score, 80)

    def test_broker_only_line_is_not_low_risk(self):
        settings = Settings()
        settings.build_quantity = 100
        line = BomLine(line_no=1, mpn="LM358DR",
                       manufacturer="Texas Instruments", quantity=1,
                       package="SOIC-8")
        subject = part(offers=[offer("Broker Co", authorized=False,
                                     stock=5_000)])
        result = LineResult(line=line, part=subject)
        result.match = Match(kind=MatchKind.EXACT_WITH_MFR, confidence=100)
        result.cost = cost_mod.cost_line(subject, 1.0, settings, FxTable())
        result.risk = risk_mod.score_line(result, settings)
        self.assertNotEqual(result.risk.level, RiskLevel.LOW)
        self.assertIn("Broker only", result.risk.flags)


class TestFractionalQuantityCeiling(unittest.TestCase):
    """``int(-(-(qty * build) // 1))`` ceils a binary float, so 0.55/board over
    100 boards required 56 pieces instead of 55."""

    def test_exact_ceiling(self):
        self.assertEqual(cost_mod.required_quantity(0.55, 100), 55)
        self.assertEqual(cost_mod.required_quantity(0.07, 100), 7)
        self.assertEqual(cost_mod.required_quantity(0.28, 25), 7)
        self.assertEqual(cost_mod.required_quantity(1.5, 3), 5)
        self.assertEqual(cost_mod.required_quantity(0.3, 10), 3)

    def test_no_mismatch_across_two_decimal_quantities(self):
        import math

        bad = []
        for cents in range(1, 200):
            quantity = cents / 100
            for build in (1, 10, 25, 50, 100, 1000):
                expected = math.ceil(Decimal(str(quantity)) * build)
                if cost_mod.required_quantity(quantity, build) != expected:
                    bad.append((quantity, build))
        self.assertEqual(bad, [])


class TestSettingsClamping(unittest.TestCase):
    """Negative risk weights were accepted and persisted, making the weighted
    denominator ~0 and producing scores like -4.9e+17 reported as "Low"."""

    def test_out_of_range_values_are_clamped(self):
        settings = Settings()
        settings.update({"weight_lifecycle": -4, "max_workers": -5,
                         "build_quantity": -10, "min_sources_ok": 0,
                         "review_confidence_threshold": 5000})
        self.assertGreaterEqual(settings.weight_lifecycle, 0)
        self.assertGreaterEqual(settings.max_workers, 1)
        self.assertGreaterEqual(settings.build_quantity, 1)
        self.assertGreaterEqual(settings.min_sources_ok, 1)
        self.assertLessEqual(settings.review_confidence_threshold, 100)

    def test_unknown_currency_is_rejected(self):
        settings = Settings()
        settings.update({"currency": "ZZZ"})
        self.assertEqual(settings.currency, "USD")

    def test_unknown_providers_are_rejected(self):
        settings = Settings()
        original = list(settings.providers)
        settings.update({"providers": ["bogus"]})
        self.assertEqual(settings.providers, original)

    def test_all_zero_weights_still_give_a_bounded_score(self):
        settings = Settings()
        for field in ("weight_lifecycle", "weight_availability",
                      "weight_sourcing", "weight_lead_time",
                      "weight_compliance", "weight_data_quality",
                      "weight_cost"):
            setattr(settings, field, 0.0)
        result = LineResult(line=BomLine(line_no=1, mpn="X", quantity=1),
                            part=None)
        result.match = Match(kind=MatchKind.NONE)
        risk = risk_mod.score_line(result, settings)
        self.assertGreaterEqual(risk.score, 0.0)
        self.assertLessEqual(risk.score, 100.0)

    def test_health_survives_zero_weights(self):
        settings = Settings()
        for field in ("weight_lifecycle", "weight_availability",
                      "weight_sourcing", "weight_lead_time",
                      "weight_compliance", "weight_data_quality"):
            setattr(settings, field, 0.0)
        result = LineResult(line=BomLine(line_no=1, mpn="X", quantity=1),
                            part=part())
        result.match = Match(kind=MatchKind.EXACT, confidence=95)
        result.risk = risk_mod.score_line(result, settings)
        health = health_mod.compute([result], settings)
        self.assertGreaterEqual(health.score, 0.0)
        self.assertLessEqual(health.score, 100.0)


class TestZeroValueValidation(unittest.TestCase):
    """The value/description cross-check divided by the parsed value, so a
    0 ohm jumper raised ZeroDivisionError and the user saw a bogus
    "rule failed" finding."""

    def test_zero_ohm_jumper_produces_no_rule_error(self):
        bom = Bom(name="t")
        bom.lines = [BomLine(line_no=1, mpn="RC0603FR-070RL", value="0",
                             description="RES SMD 0 OHM JUMPER 0603",
                             quantity=1)]
        validate(bom)
        codes = {issue.code for issue in bom.lines[0].issues}
        self.assertNotIn("rule_error", codes)


class TestLeadTimeNegation(unittest.TestCase):
    """Any text containing "stock" parsed as 0 days, so "Out of stock" was
    read as immediately available."""

    def test_negations_do_not_parse_as_in_stock(self):
        for text in ("Out of stock", "no stock", "Not in stock", "Backorder",
                     "On Order"):
            self.assertIsNone(parse_lead_time_days(text), text)

    def test_positive_forms_still_work(self):
        self.assertEqual(parse_lead_time_days("In Stock"), 0)
        self.assertEqual(parse_lead_time_days("Stock"), 0)
        self.assertEqual(parse_lead_time_days("12 weeks"), 84)


class TestRateLimiterPersistence(unittest.TestCase):
    """``set_rate`` replaced the token bucket, handing out a fresh burst every
    time a provider registry was built -- once per analysis and once per
    lookup request."""

    def test_tokens_survive_a_rate_change(self):
        client = HttpClient(rate_per_second=1.0)
        bucket = client._bucket("api.example.com", 1.0)
        self.assertTrue(bucket.acquire(timeout=2))
        spent = bucket._tokens
        client.set_rate("api.example.com", 1.0)
        same = client._bucket("api.example.com", 1.0)
        self.assertIs(same, bucket)
        self.assertLessEqual(same._tokens, spent + 0.2)


class TestCsvFormulaInjection(unittest.TestCase):
    """A BOM cell such as ``=cmd|'/c calc'!A1`` was re-exported verbatim, so
    opening the report in Excel could execute it."""

    def test_formula_cells_are_neutralised(self):
        self.assertEqual(flat.csv_safe("=cmd|'/c calc'!A1"),
                         "'=cmd|'/c calc'!A1")
        self.assertEqual(flat.csv_safe("+1+1"), "'+1+1")
        self.assertEqual(flat.csv_safe("@SUM(A1)"), "'@SUM(A1)")
        self.assertEqual(flat.csv_safe("LM358DR"), "LM358DR")
        self.assertEqual(flat.csv_safe(""), "")
        self.assertEqual(flat.csv_safe(5), 5)

    def test_exported_csv_quotes_a_formula(self):
        engine = Engine(temp_config(build_quantity=1))
        try:
            bom = Bom(name="evil")
            bom.lines = [BomLine(line_no=1, mpn="=cmd|'/c calc'!A1",
                                 quantity=1)]
            analysis = engine.analyse_bom(bom)
            path = flat.write_csv(analysis,
                                  Path(tempfile.mkdtemp()) / "evil.csv")
            text = path.read_text(encoding="utf-8-sig")
            self.assertNotIn(",=cmd", text)
            self.assertIn("'=cmd", text)
        finally:
            engine.close()


class TestServerInputHardening(unittest.TestCase):
    """A ``filename`` query parameter containing a path separator returned
    HTTP 500 and leaked the server path; a non-numeric ``limit`` on
    /api/search did the same."""

    @classmethod
    def setUpClass(cls):
        from bomiq.server.app import create_server

        cls.server = create_server(temp_config(), host="127.0.0.1", port=0)
        cls.base = cls.server.start_background()
        cls.token = cls.server.token

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.server.state["engine"].close()

    def call(self, path, method="POST", payload=None, raw=None,
             content_type=None):
        import urllib.error
        import urllib.request

        headers = {"X-BOMIQ-Token": self.token}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        elif raw is not None:
            data = raw
            if content_type:
                headers["Content-Type"] = content_type
        request = urllib.request.Request(self.base + path, data=data,
                                         headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                return exc.code, json.loads(body or b"null")
            except json.JSONDecodeError:
                return exc.code, body

    def test_filename_with_separators_does_not_500(self):
        status, payload = self.call(
            "/api/upload?filename=sub/dir/bom.csv", raw=b"MPN,Qty\nX1,1\n",
            content_type="text/csv")
        self.assertNotEqual(status, 500)
        self.assertEqual(status, 200)

    def test_traversal_filename_does_not_500(self):
        status, _ = self.call(
            "/api/upload?filename=..%2F..%2Fevil.csv", raw=b"MPN,Qty\nX1,1\n",
            content_type="text/csv")
        self.assertNotEqual(status, 500)

    def test_absurdly_long_filename_does_not_500(self):
        status, _ = self.call(
            f"/api/upload?filename={'a' * 400}.csv", raw=b"MPN,Qty\nX1,1\n",
            content_type="text/csv")
        self.assertNotEqual(status, 500)

    def test_non_numeric_search_limit_does_not_500(self):
        status, payload = self.call("/api/search",
                                    payload={"query": "res", "limit": "abc"})
        self.assertEqual(status, 200)

    def test_non_numeric_alternate_quantity_does_not_500(self):
        status, _ = self.call("/api/alternates",
                              payload={"mpn": "LM358DR", "quantity": "lots"})
        self.assertNotEqual(status, 500)

    def test_hostile_settings_are_clamped_not_persisted_raw(self):
        status, payload = self.call("/api/settings", payload={
            "weight_lifecycle": -4, "build_quantity": -10,
            "max_workers": -5, "currency": "ZZZ"})
        self.assertEqual(status, 200)
        settings = payload["settings"]
        self.assertGreaterEqual(settings["weight_lifecycle"], 0)
        self.assertGreaterEqual(settings["build_quantity"], 1)
        self.assertGreaterEqual(settings["max_workers"], 1)
        self.assertNotEqual(settings["currency"], "ZZZ")


if __name__ == "__main__":
    unittest.main()
