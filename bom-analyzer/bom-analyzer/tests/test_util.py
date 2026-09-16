"""Tests for the text, units and money primitives."""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bomiq.util.money import (  # noqa: E402
    FxTable, detect_decimal_comma, normalize_currency, order_quantity,
    parse_money, price_at_quantity,
)
from bomiq.util.text import (  # noqa: E402
    clean, collapse_refdes, manufacturer_key, mpn_root, normalize_manufacturer,
    normalize_mpn, refdes_prefix, split_list_cell, split_refdes, token_set_ratio,
)
from bomiq.util.units import (  # noqa: E402
    format_lead_time, format_value, looks_dnp, looks_no_part, mount_type,
    normalize_package, packages_equivalent, parse_lead_time_days,
    parse_quantity, parse_tolerance, parse_value, values_equivalent,
)


class TestClean(unittest.TestCase):
    def test_strips_and_collapses(self):
        self.assertEqual(clean("  RC0603 FR-07 10KL  "),
                         "RC0603 FR-07 10KL")

    def test_null_tokens_become_empty(self):
        for value in ("n/a", "N/A", "#N/A", "NULL", "none", "-", "--", "nan"):
            self.assertEqual(clean(value), "", value)

    def test_unicode_dashes_and_quotes(self):
        self.assertEqual(clean("ABC–123"), "ABC-123")
        self.assertEqual(clean("“quoted”"), '"quoted"')

    def test_integral_floats(self):
        self.assertEqual(clean(10.0), "10")
        self.assertEqual(clean(10.5), "10.5")

    def test_zero_width_removed(self):
        self.assertEqual(clean("AB​C"), "ABC")


class TestMpn(unittest.TestCase):
    def test_normalize_is_case_and_punctuation_insensitive(self):
        self.assertEqual(normalize_mpn("rc0603fr-0710kl"),
                         normalize_mpn("RC0603FR0710KL"))

    def test_root_strips_packaging_suffix(self):
        self.assertEqual(mpn_root("GRM188R71C104KA01D-TR"),
                         "GRM188R71C104KA01D")
        self.assertEqual(mpn_root("311-10.0KHRCT-ND"), "311-10.0KHRCT")

    def test_root_keeps_short_part_numbers_intact(self):
        self.assertEqual(mpn_root("SS34"), "SS34")

    def test_parenthetical_revision_dropped(self):
        self.assertEqual(normalize_mpn("B3B-EH-A(LF)(SN)"), "B3BEHA")


class TestManufacturer(unittest.TestCase):
    def test_alias_collapse(self):
        for alias in ("TI", "Texas Instruments Inc.", "texas instrument"):
            self.assertEqual(normalize_manufacturer(alias),
                             "Texas Instruments", alias)

    def test_acquisitions_map_to_acquirer(self):
        self.assertEqual(normalize_manufacturer("Linear Technology"),
                         "Analog Devices")
        self.assertEqual(normalize_manufacturer("Freescale"),
                         "NXP Semiconductors")

    def test_unknown_names_are_tidied(self):
        self.assertEqual(normalize_manufacturer("ACME ELECTRONICS CORP."),
                         "Acme")

    def test_key_matches_across_spellings(self):
        self.assertEqual(manufacturer_key("murata mfg"),
                         manufacturer_key("Murata Electronics"))


class TestRefDes(unittest.TestCase):
    def test_comma_and_semicolon(self):
        self.assertEqual(split_refdes("R1, R2;R3"), ["R1", "R2", "R3"])

    def test_ranges(self):
        self.assertEqual(split_refdes("R1-R4"), ["R1", "R2", "R3", "R4"])
        self.assertEqual(split_refdes("C10..C12"), ["C10", "C11", "C12"])
        self.assertEqual(split_refdes("U1 to U3"), ["U1", "U2", "U3"])

    def test_space_separated(self):
        self.assertEqual(split_refdes("D1 D2 D3"), ["D1", "D2", "D3"])

    def test_mixed(self):
        self.assertEqual(split_refdes("R1, R3-R5 ; C9"),
                         ["R1", "R3", "R4", "R5", "C9"])

    def test_duplicates_removed_in_order(self):
        self.assertEqual(split_refdes("R1,R1,R2"), ["R1", "R2"])

    def test_absurd_range_is_not_expanded(self):
        self.assertEqual(split_refdes("R1-R999999"), ["R1-R999999"])

    def test_collapse_round_trip(self):
        self.assertEqual(collapse_refdes(split_refdes("R1-R3, R7")),
                         "R1-R3, R7")

    def test_collapse_keeps_pairs_expanded(self):
        self.assertEqual(collapse_refdes(["C1", "C2"]), "C1, C2")

    def test_prefix(self):
        self.assertEqual(refdes_prefix("R12"), "R")
        self.assertEqual(refdes_prefix("TP3"), "TP")
        self.assertEqual(refdes_prefix("nonsense"), "")


class TestSplitList(unittest.TestCase):
    def test_separators(self):
        self.assertEqual(split_list_cell("A1; B2, C3"), ["A1", "B2", "C3"])

    def test_empty(self):
        self.assertEqual(split_list_cell("  "), [])


class TestSimilarity(unittest.TestCase):
    def test_token_set_is_order_insensitive(self):
        self.assertGreater(
            token_set_ratio("10K RES 0603", "RES 0603 10K"), 95)


class TestQuantity(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_quantity("12"), 12.0)

    def test_thousands_and_units(self):
        self.assertEqual(parse_quantity("1,200 pcs"), 1200.0)
        self.assertEqual(parse_quantity("3 ea"), 3.0)

    def test_european_decimal(self):
        self.assertEqual(parse_quantity("1,5"), 1.5)

    def test_unparseable(self):
        self.assertIsNone(parse_quantity("two"))
        self.assertIsNone(parse_quantity(""))

    def test_parenthesised_negative(self):
        self.assertEqual(parse_quantity("(4)"), -4.0)


class TestDnp(unittest.TestCase):
    def test_markers(self):
        for marker in ("DNP", "dni", "Do Not Populate", "NO POP",
                       "not fitted", "DO NOT INSTALL"):
            self.assertTrue(looks_dnp(marker), marker)

    def test_non_markers(self):
        for value in ("populate", "10K", "", "R1"):
            self.assertFalse(looks_dnp(value), value)

    def test_no_part_markers(self):
        self.assertTrue(looks_no_part("TBD"))
        self.assertTrue(looks_no_part("REF ONLY"))
        self.assertFalse(looks_no_part("LM358DR"))


class TestValues(unittest.TestCase):
    def test_rkm(self):
        self.assertEqual(parse_value("4k7"), (4700.0, "resistance"))
        self.assertEqual(parse_value("1M5")[0], 1_500_000.0)
        self.assertAlmostEqual(parse_value("1n5")[0], 1.5e-9)

    def test_uppercase_micro(self):
        self.assertAlmostEqual(parse_value("10UF")[0], 1e-5)
        self.assertEqual(parse_value("10UF")[1], "capacitance")

    def test_mega_is_context_sensitive(self):
        self.assertEqual(parse_value("10MHz")[0], 1e7)
        self.assertAlmostEqual(parse_value("10MF")[0], 1e-5)
        self.assertAlmostEqual(parse_value("10mF")[0], 1e-2)

    def test_fractional_power(self):
        self.assertAlmostEqual(parse_value("1/10W")[0], 0.1)
        self.assertEqual(parse_value("1/4W")[1], "power")

    def test_long_unit_names(self):
        self.assertEqual(parse_value("100 kOhms")[0], 100_000.0)
        self.assertEqual(parse_value("2.2 Farads")[1], "capacitance")

    def test_bare_resistance(self):
        self.assertEqual(parse_value("100R"), (100.0, "resistance"))
        self.assertEqual(parse_value("10E"), (10.0, "resistance"))

    def test_non_values(self):
        self.assertEqual(parse_value("10%")[0], None)
        self.assertEqual(parse_value("")[0], None)
        self.assertEqual(parse_value("SOIC-8")[0], None)

    def test_format_round_trip(self):
        self.assertEqual(format_value(4700.0, "resistance"), "4.7 kΩ")
        self.assertEqual(format_value(1e-7, "capacitance"), "100 nF")

    def test_equivalence(self):
        self.assertTrue(values_equivalent("4k7", "4700 Ohm"))
        self.assertFalse(values_equivalent("10UF", "0.1uF"))
        self.assertIsNone(values_equivalent("SOIC-8", "10k"))


class TestTolerance(unittest.TestCase):
    def test_percent(self):
        self.assertEqual(parse_tolerance("±1%"), 1.0)
        self.assertEqual(parse_tolerance("5 %"), 5.0)

    def test_letter_codes(self):
        self.assertEqual(parse_tolerance("J"), 5.0)
        self.assertEqual(parse_tolerance("F"), 1.0)

    def test_unknown(self):
        self.assertIsNone(parse_tolerance("tight"))


class TestPackages(unittest.TestCase):
    def test_metric_equivalence(self):
        self.assertEqual(normalize_package("1608 Metric"), "0603")
        self.assertTrue(packages_equivalent("0603", "0603 (1608 Metric)"))

    def test_ipc_names(self):
        self.assertEqual(normalize_package("RESC1608X55N"), "0603")
        self.assertEqual(normalize_package("CAPC3216X180N"), "1206")

    def test_ic_packages(self):
        self.assertEqual(normalize_package("SOIC8"), "SOIC-8")
        self.assertEqual(normalize_package("lqfp48"), "LQFP-48")

    def test_generic_tokens_are_not_comparable(self):
        self.assertIsNone(packages_equivalent("THT", "THT"))
        self.assertIsNone(packages_equivalent("SMD", "SMD"))

    def test_different_footprints(self):
        self.assertFalse(packages_equivalent("1210", "0603"))

    def test_mount_type(self):
        self.assertEqual(mount_type("0603"), "SMD")
        self.assertEqual(mount_type("DIP-16"), "THT")
        self.assertEqual(mount_type("QFN-24"), "SMD")


class TestLeadTime(unittest.TestCase):
    def test_weeks(self):
        self.assertEqual(parse_lead_time_days("12 weeks"), 84)

    def test_stock(self):
        self.assertEqual(parse_lead_time_days("In Stock"), 0)

    def test_range(self):
        self.assertEqual(parse_lead_time_days("10-14 weeks"), 84)

    def test_format(self):
        self.assertEqual(format_lead_time(0), "In stock")
        self.assertEqual(format_lead_time(84), "12 wk")
        self.assertEqual(format_lead_time(7), "7 d")


class TestMoney(unittest.TestCase):
    def test_symbols_and_codes(self):
        self.assertEqual(parse_money("$1,234.56"),
                         (Decimal("1234.56"), "USD"))
        self.assertEqual(parse_money("1.234,56 EUR")[0], Decimal("1234.56"))
        self.assertEqual(parse_money("₹ 12.50")[1], "INR")

    def test_decimal_comma_column_detection(self):
        column = ["0,0032", "0,0182", "0,029", "1,10"]
        self.assertTrue(detect_decimal_comma(column))
        self.assertEqual(parse_money("0,029", decimal_comma=True)[0],
                         Decimal("0.029"))

    def test_us_column_not_flagged(self):
        self.assertFalse(detect_decimal_comma(["1,200.00", "12.50", "3.75"]))

    def test_currency_normalisation(self):
        self.assertEqual(normalize_currency("€"), "EUR")
        self.assertEqual(normalize_currency("rs."), "INR")
        self.assertEqual(normalize_currency(""), "USD")

    def test_price_breaks(self):
        ladder = [(1, Decimal("1.00")), (10, Decimal("0.80")),
                  (100, Decimal("0.50"))]
        self.assertEqual(price_at_quantity(ladder, 1), (Decimal("1.00"), 1))
        self.assertEqual(price_at_quantity(ladder, 12), (Decimal("0.80"), 10))
        self.assertEqual(price_at_quantity(ladder, 5000),
                         (Decimal("0.50"), 100))

    def test_order_quantity_respects_moq_and_pack(self):
        self.assertEqual(order_quantity(37, moq=10, spq=25), 50)
        self.assertEqual(order_quantity(5, moq=100, spq=None), 100)
        self.assertEqual(order_quantity(100, moq=1, spq=1), 100)
        self.assertEqual(order_quantity(0, moq=100, spq=100), 0)

    def test_fx_conversion_round_trip(self):
        fx = FxTable()
        usd = Decimal("100")
        eur = fx.convert(usd, "USD", "EUR")
        back = fx.convert(eur, "EUR", "USD")
        self.assertAlmostEqual(float(back), float(usd), places=6)

    def test_fx_unknown_currency_is_reported(self):
        fx = FxTable()
        amount = fx.convert(Decimal("10"), "XYZ", "USD")
        self.assertEqual(amount, Decimal("10"))
        self.assertIn("XYZ", fx.unknown)


if __name__ == "__main__":
    unittest.main()
