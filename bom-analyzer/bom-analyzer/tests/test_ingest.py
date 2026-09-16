"""Tests for readers, header detection, column mapping and normalisation."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bomiq.core.errors import FormatError, MappingError, ReadError  # noqa: E402
from bomiq.export.xlsx_writer import Workbook  # noqa: E402
from bomiq.ingest.detect import detect_region, score_header_row  # noqa: E402
from bomiq.ingest.mapping import (  # noqa: E402
    ColumnMapper, fingerprint_headers, infer_from_values,
)
from bomiq.ingest.pipeline import IngestOptions, Ingestor  # noqa: E402
from bomiq.ingest.readers import (  # noqa: E402
    detect_encoding, read_bytes, read_file, sniff_delimiter, sniff_format,
)

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def write_temp(name: str, content: str | bytes, encoding: str = "utf-8"
               ) -> Path:
    directory = Path(tempfile.mkdtemp(prefix="bomiq-test-"))
    path = directory / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding=encoding)
    return path


# --------------------------------------------------------------------------- #
# Readers
# --------------------------------------------------------------------------- #

class TestEncodingAndDelimiter(unittest.TestCase):
    def test_utf8_bom(self):
        self.assertEqual(detect_encoding("﻿a,b".encode("utf-8-sig")),
                         "utf-8-sig")

    def test_utf16(self):
        self.assertIn("utf-16", detect_encoding("a,b".encode("utf-16")))

    def test_latin1_never_fails(self):
        encoding = detect_encoding(b"\xff\xfa\x80abc")
        self.assertTrue(encoding)

    def test_delimiter_sniffing(self):
        self.assertEqual(sniff_delimiter("a,b,c\n1,2,3\n4,5,6"), ",")
        self.assertEqual(sniff_delimiter("a;b;c\n1;2;3\n4;5;6"), ";")
        self.assertEqual(sniff_delimiter("a\tb\tc\n1\t2\t3"), "\t")
        self.assertEqual(sniff_delimiter("a|b|c\n1|2|3\n4|5|6"), "|")


class TestFormatSniffing(unittest.TestCase):
    def test_xlsx_by_magic(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("xl/workbook.xml", "<x/>")
        self.assertEqual(sniff_format(Path("mystery.dat"),
                                      buffer.getvalue()), "xlsx")

    def test_json_by_content(self):
        self.assertEqual(sniff_format(Path("f.txt"), b'[{"a":1}]'), "json")

    def test_html_by_content(self):
        self.assertEqual(sniff_format(Path("f.xls"),
                                      b"<html><table></table></html>"), "html")

    def test_extension_fallback(self):
        self.assertEqual(sniff_format(Path("f.tsv"), b"a\tb\n1\t2"),
                         "delimited")


class TestReaders(unittest.TestCase):
    def test_csv(self):
        path = write_temp("a.csv", "MPN,Qty\nLM358DR,2\n")
        sheets = read_file(path)
        self.assertEqual(len(sheets), 1)
        self.assertEqual(sheets[0].rows[0], ["MPN", "Qty"])

    def test_quoted_commas_preserved(self):
        path = write_temp("a.csv", 'MPN,Desc\nX1,"RES, 10K, 1%"\n')
        sheets = read_file(path)
        self.assertEqual(sheets[0].rows[1][1], "RES, 10K, 1%")

    def test_xlsx_round_trip_with_builtin_reader(self):
        from bomiq.ingest.readers import read_xlsx_stdlib

        wb = Workbook()
        sheet = wb.sheet("BOM")
        sheet.row(["MPN", "Qty", "Price"])
        sheet.row(["LM358DR", 2, 0.29])
        path = Path(tempfile.mkdtemp()) / "b.xlsx"
        wb.save(path)
        sheets = read_xlsx_stdlib(path.read_bytes())
        self.assertEqual(sheets[0].name, "BOM")
        self.assertEqual(sheets[0].rows[1][0], "LM358DR")
        self.assertEqual(sheets[0].rows[1][1], "2")

    def test_json_array(self):
        payload = json.dumps([{"mpn": "LM358DR", "qty": 2}])
        path = write_temp("c.json", payload)
        sheets = read_file(path)
        self.assertIn("mpn", sheets[0].rows[0])

    def test_json_nested_lines_key(self):
        payload = json.dumps({"lines": [{"mpn": "X", "qty": 1}]})
        sheets = read_bytes(payload.encode(), name="d.json")
        self.assertEqual(sheets[0].rows[1][0], "X")

    def test_html_table(self):
        html = ("<html><table><tr><th>MPN</th><th>Qty</th></tr>"
                "<tr><td>LM358DR</td><td>2</td></tr></table></html>")
        sheets = read_bytes(html.encode(), name="e.html")
        self.assertEqual(sheets[0].rows[1], ["LM358DR", "2"])

    def test_empty_file_rejected(self):
        path = write_temp("empty.csv", "")
        with self.assertRaises(ReadError):
            read_file(path)

    def test_missing_file_rejected(self):
        with self.assertRaises(ReadError):
            read_file("/definitely/not/here.csv")

    def test_corrupt_zip_gives_helpful_error(self):
        path = write_temp("bad.xlsx", b"PK\x03\x04garbage")
        with self.assertRaises(ReadError):
            read_file(path)


# --------------------------------------------------------------------------- #
# Header detection
# --------------------------------------------------------------------------- #

class TestHeaderDetection(unittest.TestCase):
    def test_header_scores_above_data(self):
        header, _ = score_header_row(["MPN", "Qty", "Manufacturer",
                                      "Description"])
        data, _ = score_header_row(["LM358DR", "2", "TI", "IC OPAMP"])
        self.assertGreater(header, data)

    def test_finds_header_below_title_block(self):
        from bomiq.ingest.readers import read_bytes as rb

        text = ("Project: Widget\nRev: A\n\n"
                "MPN,Qty,Manufacturer\nLM358DR,1,TI\n")
        sheet = rb(text.encode(), name="f.csv")[0]
        region = detect_region(sheet)
        self.assertIsNotNone(region)
        self.assertEqual(region.header_row, 3)
        self.assertEqual(region.metadata.get("Project"), "Widget")

    def test_data_row_is_never_absorbed_into_the_header(self):
        """Regression: a wide data row must not be read as a stacked header."""
        from bomiq.ingest.readers import read_bytes as rb

        text = ("Item,Qty,Manufacturer,Mfr Part Number,Description\n"
                "1,4,Yageo,RC0603FR-0710KL,RES 10K\n"
                "2,2,Murata,GRM188R71C104KA01D,CAP 100N\n")
        sheet = rb(text.encode(), name="g.csv")[0]
        region = detect_region(sheet)
        self.assertEqual(region.stacked_header_rows, 1)
        self.assertEqual(region.row_count, 2)

    def test_stacked_header_is_merged(self):
        from bomiq.ingest.readers import read_bytes as rb

        text = (",,Manufacturer,Manufacturer,\n"
                "Item,Qty,Name,Part Number,Designator\n"
                "1,4,Yageo,RC0603FR-0710KL,R1\n")
        sheet = rb(text.encode(), name="h.csv")[0]
        region = detect_region(sheet)
        # The group label above is folded into the labels below, and no data
        # row is consumed doing it.
        self.assertTrue(any("Manufacturer" in header
                            for header in region.headers))
        self.assertEqual(region.row_count, 1)

    def test_total_row_trimmed(self):
        from bomiq.ingest.readers import read_bytes as rb

        text = ("MPN,Qty\nLM358DR,1\nBAT54S,2\nTOTAL,3\n")
        sheet = rb(text.encode(), name="i.csv")[0]
        region = detect_region(sheet)
        self.assertEqual(region.row_count, 2)

    def test_section_banner_skipped(self):
        from bomiq.ingest.readers import read_bytes as rb

        text = ("MPN,Qty\n--- RESISTORS ---,\nRC0603FR-0710KL,4\n"
                "--- CAPACITORS ---,\nGRM188R71C104KA01D,2\n")
        sheet = rb(text.encode(), name="j.csv")[0]
        region = detect_region(sheet)
        rows = [cells[0] for _, cells in region.data_rows()]
        self.assertNotIn("--- RESISTORS ---", rows)
        self.assertEqual(len(rows), 2)


# --------------------------------------------------------------------------- #
# Column mapping
# --------------------------------------------------------------------------- #

class TestMapping(unittest.TestCase):
    def test_common_synonyms(self):
        mapper = ColumnMapper()
        result = mapper.map_columns(
            ["Mfr Part Number", "QTY", "Manufacturer", "Reference Designator",
             "Description"])
        self.assertEqual(result.mapping["mpn"], 0)
        self.assertEqual(result.mapping["quantity"], 1)
        self.assertEqual(result.mapping["manufacturer"], 2)
        self.assertEqual(result.mapping["ref_designators"], 3)
        self.assertEqual(result.mapping["description"], 4)
        self.assertEqual(result.missing_required, [])

    def test_content_inference_rescues_unnamed_columns(self):
        mapper = ColumnMapper()
        samples = [
            ["RC0603FR-0710KL", "GRM188R71C104KA01D", "LM358DR"],
            ["4", "2", "1"],
            ["R1", "C1", "U1"],
        ]
        result = mapper.map_columns(["", "", ""], samples=samples)
        self.assertIn("quantity", result.mapping)
        self.assertIn("ref_designators", result.mapping)

    def test_row_number_column_is_not_quantity(self):
        scores = infer_from_values([str(n) for n in range(1, 21)])
        self.assertGreater(scores.get("line_id", 0), scores.get("quantity", 0))

    def test_value_inference_requires_a_unit(self):
        self.assertNotIn("value", infer_from_values(["1200", "1300", "1400"]))
        self.assertIn("value", infer_from_values(["10k", "4k7", "100R"]))

    def test_alt_mpn_columns_collect_together(self):
        mapper = ColumnMapper()
        result = mapper.map_columns(
            ["MPN", "Qty", "Alternate PN", "Second Source PN"])
        self.assertEqual(len(result.multi_mapping.get("alt_mpns", [])), 2)

    def test_forced_mapping_wins(self):
        mapper = ColumnMapper()
        result = mapper.map_columns(["Column A", "Qty", "Column C"],
                                    forced={"mpn": 2})
        self.assertEqual(result.mapping["mpn"], 2)
        self.assertEqual(result.confidence["mpn"], 100.0)

    def test_fingerprint_is_order_insensitive(self):
        self.assertEqual(fingerprint_headers(["MPN", "Qty"]),
                         fingerprint_headers(["qty", "mpn"]))

    def test_dnp_column_detected(self):
        mapper = ColumnMapper()
        result = mapper.map_columns(["MPN", "Qty", "DNP"])
        self.assertEqual(result.mapping.get("dnp"), 2)


# --------------------------------------------------------------------------- #
# Full pipeline
# --------------------------------------------------------------------------- #

class TestPipeline(unittest.TestCase):
    def ingest(self, text: str, name: str = "t.csv", **options):
        return Ingestor().ingest_bytes(
            text.encode("utf-8"), name=name,
            options=IngestOptions(learn_templates=False, **options))

    def test_basic(self):
        result = self.ingest("MPN,Qty,Manufacturer\nLM358DR,2,TI\n")
        self.assertEqual(result.bom.line_count, 1)
        line = result.bom.lines[0]
        self.assertEqual(line.mpn, "LM358DR")
        self.assertEqual(line.quantity, 2.0)
        self.assertEqual(line.manufacturer, "Texas Instruments")

    def test_quantity_from_designators_when_blank(self):
        result = self.ingest("MPN,Qty,Reference\nLM358DR,,U1;U2;U3\n")
        self.assertEqual(result.bom.lines[0].quantity, 3.0)

    def test_blank_quantity_defaults_to_one(self):
        result = self.ingest("MPN,Qty\nLM358DR,\n")
        self.assertEqual(result.bom.lines[0].quantity, 1.0)

    def test_duplicates_merged_and_quantities_summed(self):
        result = self.ingest(
            "MPN,Qty,Manufacturer,Reference\n"
            "LM358DR,1,TI,U1\nLM358DR,2,Texas Instruments,U2;U3\n")
        self.assertEqual(result.bom.line_count, 1)
        self.assertEqual(result.bom.lines[0].quantity, 3.0)
        self.assertEqual(result.bom.lines[0].ref_text, "U1-U3")

    def test_merge_can_be_disabled(self):
        result = self.ingest(
            "MPN,Qty\nLM358DR,1\nLM358DR,2\n", merge_duplicate_mpns=False)
        self.assertEqual(result.bom.line_count, 2)

    def test_dnp_is_detected_from_a_fit_column(self):
        result = self.ingest("MPN,Qty,Fit?\nLM358DR,1,N\nBAT54S,1,Y\n")
        lines = {line.mpn: line.dnp for line in result.bom.lines}
        self.assertTrue(lines["LM358DR"])
        self.assertFalse(lines["BAT54S"])

    def test_dnp_marker_in_the_mpn_column_is_not_a_part_number(self):
        result = self.ingest("MPN,Qty,Description\nDNP,1,MOUNTING HOLE\n")
        line = result.bom.lines[0]
        self.assertTrue(line.dnp)
        self.assertEqual(line.mpn, "")

    def test_excel_error_values_are_flagged(self):
        result = self.ingest("MPN,Qty\n#N/A,1\n1.23E+09,1\n")
        codes = {issue.code for line in result.bom.lines
                 for issue in line.issues}
        self.assertTrue({"mpn_missing", "mpn_scientific"} & codes)

    def test_refdes_conflict_is_reported(self):
        result = self.ingest(
            "MPN,Qty,Reference\nLM358DR,1,U1\nBAT54S,1,U1\n")
        codes = {issue.code for issue in result.bom.issues}
        self.assertIn("refdes_conflict", codes)

    def test_missing_required_columns_raises(self):
        with self.assertRaises(MappingError):
            self.ingest("Colour,Shape\nred,round\n")

    def test_no_table_raises(self):
        with self.assertRaises((FormatError, MappingError)):
            self.ingest("just one line of prose\n")

    def test_raw_columns_are_preserved(self):
        result = self.ingest(
            "MPN,Qty,Internal Notes\nLM358DR,1,keep me\n")
        self.assertEqual(result.bom.lines[0].raw["Internal Notes"], "keep me")

    def test_unmapped_columns_recorded(self):
        result = self.ingest("MPN,Qty,Warehouse Bin\nLM358DR,1,A12\n")
        self.assertIn("Warehouse Bin", result.bom.unmapped_columns)

    def test_forced_sheet_name_unknown_raises(self):
        with self.assertRaises(FormatError):
            self.ingest("MPN,Qty\nLM358DR,1\n", sheet_name="Nope")


class TestSampleFiles(unittest.TestCase):
    """Every sample file must ingest cleanly."""

    @classmethod
    def setUpClass(cls):
        if not (SAMPLES / "01-altium-export.csv").exists():
            import subprocess

            subprocess.run([sys.executable,
                            str(SAMPLES / "make_samples.py")], check=True,
                           capture_output=True)

    def ingest(self, name: str):
        return Ingestor().ingest_file(
            SAMPLES / name, IngestOptions(learn_templates=False))

    def test_altium_export(self):
        result = self.ingest("01-altium-export.csv")
        self.assertGreaterEqual(result.bom.line_count, 12)
        self.assertEqual(result.bom.metadata.get("Project"), "Sensor Hub")
        first = result.bom.lines[0]
        self.assertEqual(first.mpn, "RC0603FR-0710KL")
        self.assertEqual(len(first.ref_designators), 4)
        self.assertIsNotNone(first.unit_price_in)

    def test_messy_handmade(self):
        result = self.ingest("02-messy-handmade.csv")
        self.assertGreaterEqual(result.bom.line_count, 9)
        # European decimal commas
        prices = [line.unit_price_in for line in result.bom.lines
                  if line.unit_price_in is not None]
        self.assertTrue(all(p < 100 for p in prices), prices)
        # currency from the header
        self.assertIn("EUR", {line.currency_in for line in result.bom.lines})
        # inverted fit column
        self.assertTrue(any(line.dnp for line in result.bom.lines))

    def test_erp_extract(self):
        result = self.ingest("03-erp-extract.tsv")
        self.assertEqual(result.bom.line_count, 5)
        self.assertTrue(all(line.internal_pn for line in result.bom.lines))

    def test_excel_damaged(self):
        result = self.ingest("04-excel-damaged.csv")
        severities = {issue.severity.value for issue in result.bom.issues}
        self.assertIn("error", severities)

    def test_multi_sheet_workbook(self):
        result = self.ingest("05-multi-sheet.xlsx")
        self.assertEqual(len(result.bom.sheets_read), 2)
        self.assertEqual(result.bom.line_count, 10)

    def test_json_bom(self):
        result = self.ingest("06-iot-gateway.json")
        self.assertEqual(result.bom.line_count, 6)
        line = next(line for line in result.bom.lines
                    if line.mpn == "ESP32-WROOM-32E")
        self.assertTrue(line.alt_mpns)

    def test_semicolon_german(self):
        result = self.ingest("07-semicolon-german.csv")
        self.assertEqual(result.bom.line_count, 3)
        self.assertEqual(result.bom.lines[0].mpn, "150060GS75000")
        self.assertEqual(len(result.bom.lines[0].ref_designators), 4)


if __name__ == "__main__":
    unittest.main()
