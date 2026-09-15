"""
File readers: turn any BOM file into a list of :class:`RawSheet` grids.

Supported without any third-party package
-----------------------------------------
``.csv .tsv .txt .psv`` -- delimiter and encoding sniffing
``.xlsx .xlsm``         -- pure-stdlib reader (zipfile + ElementTree)
``.ods``                -- pure-stdlib reader (zipfile + ElementTree)
``.json``               -- array-of-objects or ``{"lines": [...]}``
``.htm .html``          -- first/largest ``<table>``
``.xml``                -- generic flat XML records

Supported when the optional package is installed
------------------------------------------------
``.xls`` (legacy BIFF) -- needs ``xlrd``; a clear, actionable error otherwise.

Everything returns *strings*. Type coercion is the normaliser's job, so that a
part number like ``0402`` or ``1E5`` is never silently turned into a float.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable, Sequence
from xml.etree import ElementTree as ET

from ..core.errors import FormatError, ReadError
from ..util import log
from ..util.text import clean

LOG = log.get("ingest.readers")

MAX_ROWS = 200_000
MAX_COLS = 512
CSV_SNIFF_BYTES = 128 * 1024

TEXT_EXTENSIONS = {".csv", ".tsv", ".txt", ".psv", ".dat", ".bom"}
EXCEL_ZIP_EXTENSIONS = {".xlsx", ".xlsm", ".xltx", ".xltm"}
LEGACY_EXCEL_EXTENSIONS = {".xls", ".xlt"}
OPENDOC_EXTENSIONS = {".ods", ".fods", ".ots"}
JSON_EXTENSIONS = {".json", ".jsonl", ".ndjson"}
HTML_EXTENSIONS = {".htm", ".html", ".xhtml"}
XML_EXTENSIONS = {".xml"}

SUPPORTED_EXTENSIONS = (
    TEXT_EXTENSIONS | EXCEL_ZIP_EXTENSIONS | LEGACY_EXCEL_EXTENSIONS
    | OPENDOC_EXTENSIONS | JSON_EXTENSIONS | HTML_EXTENSIONS | XML_EXTENSIONS
)


@dataclass
class RawSheet:
    """A rectangular grid of strings plus where it came from."""

    name: str
    rows: list[list[str]] = field(default_factory=list)
    source_format: str = ""
    notes: list[str] = field(default_factory=list)
    # 0-based row offset of ``rows[0]`` within the original sheet, used so that
    # reported row numbers match what the user sees in Excel.
    row_offset: int = 0

    @property
    def height(self) -> int:
        return len(self.rows)

    @property
    def width(self) -> int:
        return max((len(row) for row in self.rows), default=0)

    def non_empty_row_count(self) -> int:
        return sum(1 for row in self.rows if any(clean(cell) for cell in row))

    def rectangular(self) -> list[list[str]]:
        width = self.width
        return [list(row) + [""] * (width - len(row)) for row in self.rows]


# --------------------------------------------------------------------------- #
# Encoding detection
# --------------------------------------------------------------------------- #

_BOMS: list[tuple[bytes, str]] = [
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
]

_CANDIDATE_ENCODINGS = ("utf-8", "cp1252", "latin-1", "cp1251", "shift_jis",
                        "gb18030", "utf-16")


def detect_encoding(raw: bytes) -> str:
    """Best-effort text encoding detection with a safe last resort."""
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            return encoding
    sample = raw[:CSV_SNIFF_BYTES]
    # A lot of NUL bytes at odd/even positions means UTF-16 without a BOM.
    if sample.count(b"\x00") > len(sample) * 0.2:
        evens = sample[0::2].count(0)
        odds = sample[1::2].count(0)
        return "utf-16-le" if odds > evens else "utf-16-be"
    try:  # pragma: no cover - optional accelerator
        import charset_normalizer

        best = charset_normalizer.from_bytes(sample).best()
        if best and best.encoding:
            return best.encoding
    except Exception:
        pass
    try:  # pragma: no cover - optional accelerator
        import chardet

        guess = chardet.detect(sample)
        if guess and guess.get("encoding") and (guess.get("confidence") or 0) > 0.7:
            return str(guess["encoding"])
    except Exception:
        pass
    for encoding in _CANDIDATE_ENCODINGS:
        try:
            sample.decode(encoding)
            return encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return "latin-1"  # never fails


def decode_text(raw: bytes) -> tuple[str, str]:
    encoding = detect_encoding(raw)
    try:
        text = raw.decode(encoding, errors="replace")
    except LookupError:  # pragma: no cover
        encoding = "latin-1"
        text = raw.decode(encoding, errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n"), encoding


# --------------------------------------------------------------------------- #
# Delimited text
# --------------------------------------------------------------------------- #

_DELIMITERS = [",", ";", "\t", "|", "^", ":"]


def sniff_delimiter(text: str) -> str:
    """Pick the delimiter that yields the most consistent column count."""
    lines = [line for line in text.split("\n")[:200] if line.strip()]
    if not lines:
        return ","
    try:
        dialect = csv.Sniffer().sniff("\n".join(lines[:50]),
                                      delimiters="".join(_DELIMITERS))
        if dialect.delimiter in _DELIMITERS:
            return dialect.delimiter
    except csv.Error:
        pass
    best_delimiter, best_score = ",", -1.0
    for delimiter in _DELIMITERS:
        counts = [len(next(csv.reader([line], delimiter=delimiter), []))
                  for line in lines[:60]]
        counts = [c for c in counts if c > 0]
        if not counts:
            continue
        # Favour many columns and low variance.
        mode = max(set(counts), key=counts.count)
        if mode < 2:
            continue
        consistency = counts.count(mode) / len(counts)
        score = consistency * 10 + min(mode, 30) * 0.4
        if score > best_score:
            best_delimiter, best_score = delimiter, score
    if best_score < 0:
        # Fixed-width or single column: try runs of 2+ spaces.
        if any(re.search(r"\S {2,}\S", line) for line in lines[:20]):
            return "  "
        return ","
    return best_delimiter


def read_delimited(raw: bytes, name: str = "Sheet1") -> list[RawSheet]:
    text, encoding = decode_text(raw)
    if not text.strip():
        raise FormatError("The file is empty.")
    delimiter = sniff_delimiter(text)
    rows: list[list[str]] = []
    if delimiter == "  ":
        for line in text.split("\n"):
            if not line.strip():
                rows.append([])
                continue
            rows.append([cell.strip() for cell in re.split(r"\s{2,}", line.strip())])
    else:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter,
                            quotechar='"', skipinitialspace=True)
        for row in reader:
            rows.append([clean(cell) for cell in row[:MAX_COLS]])
            if len(rows) >= MAX_ROWS:
                LOG.warning("Truncating input at %d rows", MAX_ROWS)
                break
    sheet = RawSheet(name=name, rows=rows, source_format="delimited")
    sheet.notes.append(
        f"Encoding {encoding}, delimiter "
        f"{'multi-space' if delimiter == '  ' else repr(delimiter)}")
    return [sheet]


# --------------------------------------------------------------------------- #
# XLSX / XLSM (pure stdlib)
# --------------------------------------------------------------------------- #

_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_NS_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

_EXCEL_EPOCH = datetime(1899, 12, 30)


def _col_to_index(reference: str) -> int:
    """``'C'`` -> 2, ``'AB12'`` -> 27."""
    index = 0
    for char in reference:
        if char.isalpha():
            index = index * 26 + (ord(char.upper()) - 64)
        else:
            break
    return max(0, index - 1)


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        data = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    strings: list[str] = []
    for si in ET.fromstring(data).iter(f"{_NS_MAIN}si"):
        pieces: list[str] = []
        for node in si.iter():
            if node.tag == f"{_NS_MAIN}t":
                # Skip phonetic runs (<rPh><t>) which duplicate content.
                pieces.append(node.text or "")
        strings.append("".join(pieces))
    return strings


def _xlsx_date_formats(archive: zipfile.ZipFile) -> set[int]:
    """Style indices whose number format renders as a date or time."""
    try:
        data = archive.read("xl/styles.xml")
    except KeyError:
        return set()
    try:
        root = ET.fromstring(data)
    except ET.ParseError:  # pragma: no cover
        return set()
    builtin_date_ids = set(range(14, 23)) | set(range(45, 48)) | {27, 30, 36, 50, 57}
    custom_date_ids: set[int] = set()
    for fmt in root.iter(f"{_NS_MAIN}numFmt"):
        code = (fmt.get("formatCode") or "").lower()
        fmt_id = fmt.get("numFmtId")
        if fmt_id and re.search(r"[dmyh]", code) and "0.00" not in code:
            try:
                custom_date_ids.add(int(fmt_id))
            except ValueError:
                continue
    date_style_indices: set[int] = set()
    cell_xfs = root.find(f"{_NS_MAIN}cellXfs")
    if cell_xfs is None:
        return set()
    for index, xf in enumerate(cell_xfs.findall(f"{_NS_MAIN}xf")):
        try:
            fmt_id = int(xf.get("numFmtId") or 0)
        except ValueError:
            continue
        if fmt_id in builtin_date_ids or fmt_id in custom_date_ids:
            date_style_indices.add(index)
    return date_style_indices


def _xlsx_sheet_list(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Return ``[(sheet_name, zip_path)]`` in workbook order, visible first."""
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    except KeyError:
        return []
    relationships: dict[str, str] = {}
    try:
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        for rel in rels:
            rel_id = rel.get("Id")
            target = rel.get("Target") or ""
            if rel_id:
                target = target.lstrip("/")
                if not target.startswith("xl/"):
                    target = "xl/" + target.replace("../", "")
                relationships[rel_id] = target
    except KeyError:  # pragma: no cover
        pass

    sheets: list[tuple[str, str]] = []
    hidden: list[tuple[str, str]] = []
    for sheet in workbook.iter(f"{_NS_MAIN}sheet"):
        name = sheet.get("name") or f"Sheet{len(sheets) + 1}"
        rel_id = sheet.get(f"{_NS_REL}id")
        path = relationships.get(rel_id or "")
        if not path:
            continue
        if path not in archive.namelist():
            alternative = f"xl/worksheets/{Path(path).name}"
            path = alternative if alternative in archive.namelist() else path
        entry = (name, path)
        if (sheet.get("state") or "visible") != "visible":
            hidden.append(entry)
        else:
            sheets.append(entry)
    return sheets + hidden


def _format_number(text: str) -> str:
    """Render a numeric cell without losing significant digits."""
    try:
        number = float(text)
    except (TypeError, ValueError):
        return text
    if number.is_integer() and abs(number) < 1e15:
        return str(int(number))
    formatted = repr(round(number, 10))
    return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted


def _excel_serial_to_text(text: str) -> str:
    try:
        serial = float(text)
    except (TypeError, ValueError):
        return text
    try:
        stamp = _EXCEL_EPOCH + __import__("datetime").timedelta(days=serial)
    except (OverflowError, OSError, ValueError):  # pragma: no cover
        return text
    if serial < 1:
        return stamp.strftime("%H:%M:%S")
    if abs(serial - int(serial)) < 1e-9:
        return stamp.strftime("%Y-%m-%d")
    return stamp.strftime("%Y-%m-%d %H:%M:%S")


def read_xlsx_stdlib(raw: bytes, multi_sheet: bool = True) -> list[RawSheet]:
    """Read an OOXML workbook using only the standard library."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ReadError(
            "This .xlsx file appears to be corrupt or is actually an older .xls "
            "file with the wrong extension."
        ) from exc

    with archive:
        shared = _xlsx_shared_strings(archive)
        date_styles = _xlsx_date_formats(archive)
        sheet_entries = _xlsx_sheet_list(archive)
        if not sheet_entries:
            sheet_entries = [
                (Path(name).stem, name) for name in archive.namelist()
                if name.startswith("xl/worksheets/") and name.endswith(".xml")
            ]
        if not sheet_entries:
            raise FormatError("No worksheets were found in this workbook.")

        sheets: list[RawSheet] = []
        for sheet_name, path in sheet_entries:
            try:
                data = archive.read(path)
            except KeyError:  # pragma: no cover
                continue
            rows = _parse_xlsx_sheet(data, shared, date_styles)
            sheet = RawSheet(name=sheet_name, rows=rows, source_format="xlsx")
            sheets.append(sheet)
            if not multi_sheet and sheet.non_empty_row_count() > 1:
                break
        if not sheets:
            raise FormatError("The workbook contains no readable worksheet.")
        return sheets


def _parse_xlsx_sheet(data: bytes, shared: Sequence[str],
                      date_styles: set[int]) -> list[list[str]]:
    rows: list[list[str]] = []
    current_row: list[str] = []
    current_index = 0
    cell_ref = ""
    cell_type = ""
    style_index = 0
    value_parts: list[str] = []
    is_inline = False

    for event, element in ET.iterparse(io.BytesIO(data), events=("start", "end")):
        tag = element.tag
        if event == "start":
            if tag == f"{_NS_MAIN}row":
                current_row = []
                try:
                    declared = int(element.get("r") or 0)
                except ValueError:
                    declared = 0
                # Honour gaps in the row index so alignment is preserved.
                if declared and declared > len(rows) + 1:
                    while len(rows) < declared - 1:
                        rows.append([])
                current_index = declared
            elif tag == f"{_NS_MAIN}c":
                cell_ref = element.get("r") or ""
                cell_type = element.get("t") or "n"
                try:
                    style_index = int(element.get("s") or -1)
                except ValueError:
                    style_index = -1
                value_parts = []
                is_inline = False
            elif tag == f"{_NS_MAIN}is":
                is_inline = True
        else:  # end
            # Text must be read on the *end* event. On ``start`` the parser has
            # only seen the opening tag, so a value whose text node straddles
            # an iterparse read boundary is not populated yet and would be
            # silently lost -- which showed up as blank cells scattered
            # through large workbooks.
            if tag in (f"{_NS_MAIN}v", f"{_NS_MAIN}t"):
                if element.text:
                    value_parts.append(element.text)
            elif tag == f"{_NS_MAIN}c":
                text = "".join(value_parts)
                if cell_type == "s":
                    try:
                        text = shared[int(text)] if text != "" else ""
                    except (ValueError, IndexError):
                        text = ""
                elif cell_type in ("inlineStr",) or is_inline:
                    pass
                elif cell_type == "b":
                    text = "TRUE" if text in ("1", "true", "TRUE") else "FALSE"
                elif cell_type == "e":
                    text = ""
                elif cell_type in ("n", "") and text:
                    if style_index in date_styles:
                        text = _excel_serial_to_text(text)
                    else:
                        text = _format_number(text)
                column = _col_to_index(cell_ref) if cell_ref else len(current_row)
                if column >= MAX_COLS:
                    element.clear()
                    continue
                while len(current_row) <= column:
                    current_row.append("")
                current_row[column] = clean(text)
                element.clear()
            elif tag == f"{_NS_MAIN}row":
                if current_index and current_index == len(rows) + 1:
                    rows.append(current_row)
                else:
                    rows.append(current_row)
                current_row = []
                element.clear()
                if len(rows) >= MAX_ROWS:
                    break
            elif tag == f"{_NS_MAIN}sheetData":
                element.clear()
    while rows and not any(clean(cell) for cell in rows[-1]):
        rows.pop()
    return rows


def read_xlsx(raw: bytes, multi_sheet: bool = True) -> list[RawSheet]:
    """Read an xlsx/xlsm workbook, preferring openpyxl when it is installed."""
    try:  # pragma: no cover - exercised only when openpyxl present
        import openpyxl

        workbook = openpyxl.load_workbook(
            io.BytesIO(raw), read_only=True, data_only=True, keep_links=False)
        sheets: list[RawSheet] = []
        for worksheet in workbook.worksheets:
            rows: list[list[str]] = []
            for row in worksheet.iter_rows(values_only=True):
                rows.append([_coerce_cell(value) for value in row[:MAX_COLS]])
                if len(rows) >= MAX_ROWS:
                    break
            while rows and not any(clean(cell) for cell in rows[-1]):
                rows.pop()
            sheets.append(RawSheet(name=worksheet.title, rows=rows,
                                   source_format="xlsx"))
            if not multi_sheet and rows:
                break
        workbook.close()
        if sheets:
            return sheets
    except ImportError:
        pass
    except Exception as exc:
        LOG.info("openpyxl failed (%s); falling back to the built-in reader.", exc)
    return read_xlsx_stdlib(raw, multi_sheet=multi_sheet)


def _coerce_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (datetime, date, time)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) \
            else value.isoformat()
    if isinstance(value, float):
        return _format_number(repr(value))
    return clean(value)


# --------------------------------------------------------------------------- #
# Legacy .xls
# --------------------------------------------------------------------------- #

def read_xls(raw: bytes, multi_sheet: bool = True) -> list[RawSheet]:
    # Some tools emit xlsx/CSV/HTML with an .xls extension; detect and reroute.
    if raw[:4] == b"PK\x03\x04":
        return read_xlsx(raw, multi_sheet=multi_sheet)
    head = raw[:512].lstrip().lower()
    if head.startswith((b"<html", b"<!doctype html", b"<table", b"<?xml")):
        return read_html(raw)
    try:  # pragma: no cover - optional dependency
        import xlrd

        book = xlrd.open_workbook(file_contents=raw)
        sheets: list[RawSheet] = []
        for worksheet in book.sheets():
            rows: list[list[str]] = []
            for row_index in range(min(worksheet.nrows, MAX_ROWS)):
                values = worksheet.row_values(row_index)[:MAX_COLS]
                rows.append([_coerce_cell(value) for value in values])
            sheets.append(RawSheet(name=worksheet.name, rows=rows,
                                   source_format="xls"))
            if not multi_sheet and rows:
                break
        return sheets
    except ImportError as exc:
        raise ReadError(
            "Legacy .xls files need the optional 'xlrd' package "
            "(pip install \"xlrd>=2.0.1\"). Alternatively, open the file in "
            "Excel or LibreOffice and save it as .xlsx or .csv — Schemata "
            "reads those with no extra packages."
        ) from exc
    except Exception as exc:
        raise ReadError(f"This .xls file could not be read: {exc}") from exc


# --------------------------------------------------------------------------- #
# OpenDocument .ods
# --------------------------------------------------------------------------- #

_NS_ODS_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
_NS_ODS_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_NS_ODS_OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"


def read_ods(raw: bytes, multi_sheet: bool = True) -> list[RawSheet]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        # Flat ODF (.fods) is plain XML.
        return _read_ods_xml(raw, multi_sheet=multi_sheet)
    with archive:
        try:
            content = archive.read("content.xml")
        except KeyError as exc:
            raise FormatError("This .ods file has no content.xml.") from exc
    return _read_ods_xml(content, multi_sheet=multi_sheet)


def _read_ods_xml(content: bytes, multi_sheet: bool = True) -> list[RawSheet]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ReadError(f"This spreadsheet could not be parsed: {exc}") from exc
    sheets: list[RawSheet] = []
    for table in root.iter(f"{_NS_ODS_TABLE}table"):
        name = table.get(f"{_NS_ODS_TABLE}name") or f"Sheet{len(sheets) + 1}"
        rows: list[list[str]] = []
        for row in table.iter(f"{_NS_ODS_TABLE}table-row"):
            repeat_rows = int(row.get(f"{_NS_ODS_TABLE}number-rows-repeated") or 1)
            repeat_rows = min(repeat_rows, 1000)
            values: list[str] = []
            # Covered cells (the ones hidden under a horizontal merge) must be
            # emitted as blanks, otherwise every cell to the right of a merged
            # cell shifts one column left and lands in the wrong field.
            for cell in row:
                if cell.tag == f"{_NS_ODS_TABLE}covered-table-cell":
                    covered = int(
                        cell.get(f"{_NS_ODS_TABLE}number-columns-repeated") or 1)
                    values.extend([""] * min(covered, MAX_COLS))
                    continue
                if cell.tag != f"{_NS_ODS_TABLE}table-cell":
                    continue
                repeat = int(
                    cell.get(f"{_NS_ODS_TABLE}number-columns-repeated") or 1)
                repeat = min(repeat, MAX_COLS)
                text_value = cell.get(f"{_NS_ODS_OFFICE}value")
                if text_value is None:
                    text_value = cell.get(f"{_NS_ODS_OFFICE}date-value") \
                        or cell.get(f"{_NS_ODS_OFFICE}boolean-value")
                if text_value is None:
                    pieces = [node.text or "" for node in
                              cell.iter(f"{_NS_ODS_TEXT}p")]
                    text_value = "\n".join(p for p in pieces if p)
                elif cell.get(f"{_NS_ODS_OFFICE}value-type") == "float":
                    text_value = _format_number(text_value)
                values.extend([clean(text_value)] * repeat)
                if len(values) >= MAX_COLS:
                    break
            while values and values[-1] == "":
                values.pop()
            for _ in range(repeat_rows):
                rows.append(list(values))
                if len(rows) >= MAX_ROWS:
                    break
        while rows and not any(rows[-1]):
            rows.pop()
        sheets.append(RawSheet(name=name, rows=rows, source_format="ods"))
        if not multi_sheet and rows:
            break
    if not sheets:
        raise FormatError("No tables were found in this document.")
    return sheets


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #

def read_json(raw: bytes, name: str = "json") -> list[RawSheet]:
    text, _ = decode_text(raw)
    text = text.strip()
    records: list[dict[str, Any]] = []
    if not text:
        raise FormatError("The JSON file is empty.")
    if text.lstrip().startswith("{") and "\n{" in text:
        # newline-delimited JSON
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
    if not records:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ReadError(f"Invalid JSON: {exc}") from exc
        if isinstance(payload, list):
            records = [item for item in payload if isinstance(item, dict)]
        elif isinstance(payload, dict):
            for key in ("lines", "bom", "items", "parts", "rows", "data",
                        "components", "BomLines"):
                value = payload.get(key)
                if isinstance(value, list):
                    records = [item for item in value if isinstance(item, dict)]
                    break
            else:
                # dict of dicts
                values = [v for v in payload.values() if isinstance(v, dict)]
                if values:
                    records = values
    if not records:
        raise FormatError(
            "No array of BOM line objects was found in this JSON file.")
    headers: list[str] = []
    for record in records:
        for key in record:
            if key not in headers:
                headers.append(str(key))
    rows = [headers]
    for record in records:
        rows.append([_flatten_json_value(record.get(header)) for header in headers])
    return [RawSheet(name=name, rows=rows, source_format="json")]


def _flatten_json_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(_flatten_json_value(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, separators=(",", ":"))
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return clean(value)


# --------------------------------------------------------------------------- #
# HTML / XML
# --------------------------------------------------------------------------- #

_TAG_RE = re.compile(r"<[^>]+>")
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", re.IGNORECASE | re.DOTALL)
_TABLE_RE = re.compile(r"<table\b[^>]*>(.*?)</table>", re.IGNORECASE | re.DOTALL)
_ENTITY_RE = re.compile(r"&(#?\w+);")


def read_html(raw: bytes) -> list[RawSheet]:
    import html as html_module

    text, _ = decode_text(raw)
    tables = _TABLE_RE.findall(text) or [text]
    sheets: list[RawSheet] = []
    for index, table_html in enumerate(tables):
        rows: list[list[str]] = []
        for row_html in _ROW_RE.findall(table_html):
            cells = _CELL_RE.findall(row_html)
            if not cells:
                continue
            rows.append([
                clean(html_module.unescape(_TAG_RE.sub(" ", cell)))
                for cell in cells[:MAX_COLS]
            ])
        if rows:
            sheets.append(RawSheet(name=f"Table{index + 1}", rows=rows,
                                   source_format="html"))
    if not sheets:
        raise FormatError("No HTML table was found in this file.")
    # Largest table first -- that is nearly always the BOM.
    sheets.sort(key=lambda sheet: sheet.non_empty_row_count(), reverse=True)
    return sheets


def read_xml(raw: bytes) -> list[RawSheet]:
    text, _ = decode_text(raw)
    lowered = text[:2000].lower()
    if "<table" in lowered or "spreadsheetml" in lowered or "<html" in lowered:
        if "spreadsheetml" in lowered:
            return _read_spreadsheetml(text)
        return read_html(raw)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ReadError(f"Invalid XML: {exc}") from exc

    # Find the repeated element that looks like a record list.
    counts: dict[str, list[ET.Element]] = {}
    for parent in root.iter():
        for child in list(parent):
            counts.setdefault(child.tag, []).append(child)
    if not counts:
        raise FormatError("No repeated records were found in this XML file.")
    tag, elements = max(counts.items(), key=lambda item: len(item[1]))
    if len(elements) < 2:
        raise FormatError("No repeated records were found in this XML file.")

    headers: list[str] = []
    records: list[dict[str, str]] = []
    for element in elements:
        record: dict[str, str] = {}
        for key, value in element.attrib.items():
            record[_strip_ns(key)] = clean(value)
        for child in element:
            key = _strip_ns(child.tag)
            value = clean(child.text)
            if not value and child.attrib:
                value = clean(next(iter(child.attrib.values())))
            record[key] = value
        if clean(element.text) and not record:
            record["value"] = clean(element.text)
        for key in record:
            if key not in headers:
                headers.append(key)
        records.append(record)
    rows = [headers] + [[record.get(h, "") for h in headers] for record in records]
    return [RawSheet(name=_strip_ns(tag), rows=rows, source_format="xml")]


def _strip_ns(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _read_spreadsheetml(text: str) -> list[RawSheet]:
    """Excel 2003 XML (SpreadsheetML) workbooks."""
    ns = {"ss": "urn:schemas-microsoft-com:office:spreadsheet"}
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ReadError(f"Invalid SpreadsheetML: {exc}") from exc
    sheets: list[RawSheet] = []
    for worksheet in root.findall(".//ss:Worksheet", ns):
        name = worksheet.get(f"{{{ns['ss']}}}Name") or f"Sheet{len(sheets) + 1}"
        rows: list[list[str]] = []
        for row in worksheet.findall(".//ss:Row", ns):
            values: list[str] = []
            for cell in row.findall("ss:Cell", ns):
                index_attr = cell.get(f"{{{ns['ss']}}}Index")
                if index_attr:
                    try:
                        target = int(index_attr) - 1
                        while len(values) < target:
                            values.append("")
                    except ValueError:
                        pass
                data = cell.find("ss:Data", ns)
                values.append(clean(data.text) if data is not None else "")
            rows.append(values)
        sheets.append(RawSheet(name=name, rows=rows,
                               source_format="spreadsheetml"))
    if not sheets:
        raise FormatError("No worksheet was found in this SpreadsheetML file.")
    return sheets


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def sniff_format(path: Path, raw: bytes) -> str:
    """Decide how to read a file from its magic bytes, then its extension."""
    if raw[:4] == b"PK\x03\x04":
        names: set[str] = set()
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                names = set(archive.namelist())
        except zipfile.BadZipFile:  # pragma: no cover
            pass
        if any(name.startswith("xl/") for name in names):
            return "xlsx"
        if "content.xml" in names and "mimetype" in names:
            return "ods"
    if raw[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "xls"
    head = raw[:1024].lstrip()
    lowered = head.lower()
    if lowered.startswith((b"<html", b"<!doctype html")):
        return "html"
    if lowered.startswith(b"<?xml") or lowered.startswith(b"<"):
        return "xml"
    if head[:1] in (b"{", b"["):
        return "json"

    suffix = path.suffix.lower()
    if suffix in EXCEL_ZIP_EXTENSIONS:
        return "xlsx"
    if suffix in LEGACY_EXCEL_EXTENSIONS:
        return "xls"
    if suffix in OPENDOC_EXTENSIONS:
        return "ods"
    if suffix in JSON_EXTENSIONS:
        return "json"
    if suffix in HTML_EXTENSIONS:
        return "html"
    if suffix in XML_EXTENSIONS:
        return "xml"
    return "delimited"


def read_file(path: str | Path, multi_sheet: bool = True) -> list[RawSheet]:
    """Read any supported BOM file into one or more :class:`RawSheet` grids."""
    path = Path(path)
    if not path.exists():
        raise ReadError(f"File not found: {path}")
    if path.is_dir():
        raise ReadError(f"{path} is a folder, not a file.")
    size = path.stat().st_size
    if size == 0:
        raise ReadError("The file is empty.")
    if size > 200 * 1024 * 1024:
        raise ReadError("Files larger than 200 MB are not supported.")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReadError(f"Could not open {path.name}: {exc}") from exc
    return read_bytes(raw, name=path.name, multi_sheet=multi_sheet,
                      hint_path=path)


def read_bytes(raw: bytes, name: str = "input", multi_sheet: bool = True,
               hint_path: Path | None = None) -> list[RawSheet]:
    """Read BOM content already in memory (used by the HTTP upload route)."""
    path = hint_path or Path(name)
    kind = sniff_format(path, raw)
    LOG.info("Reading %s as %s (%d bytes)", name, kind, len(raw))
    if kind == "xlsx":
        sheets = read_xlsx(raw, multi_sheet=multi_sheet)
    elif kind == "xls":
        sheets = read_xls(raw, multi_sheet=multi_sheet)
    elif kind == "ods":
        sheets = read_ods(raw, multi_sheet=multi_sheet)
    elif kind == "json":
        sheets = read_json(raw, name=Path(name).stem or "json")
    elif kind == "html":
        sheets = read_html(raw)
    elif kind == "xml":
        sheets = read_xml(raw)
    else:
        sheets = read_delimited(raw, name=Path(name).stem or "Sheet1")
    for sheet in sheets:
        sheet.source_format = sheet.source_format or kind
    return [sheet for sheet in sheets if sheet.rows] or sheets


def describe_support() -> dict[str, Any]:
    """What this install can read, for the UI's help panel."""
    try:  # pragma: no cover
        import openpyxl  # noqa: F401

        xlsx_engine = "openpyxl"
    except ImportError:
        xlsx_engine = "built-in"
    try:  # pragma: no cover
        import xlrd  # noqa: F401

        xls_available = True
    except ImportError:
        xls_available = False
    return {
        "extensions": sorted(SUPPORTED_EXTENSIONS),
        "xlsx_engine": xlsx_engine,
        "legacy_xls": xls_available,
        "notes": [] if xls_available else [
            "Legacy .xls needs the optional 'xlrd' package; .xlsx and .csv "
            "work out of the box."
        ],
    }


def iter_cells(sheets: Iterable[RawSheet]) -> Iterable[str]:
    for sheet in sheets:
        for row in sheet.rows:
            for cell in row:
                yield cell
