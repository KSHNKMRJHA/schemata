"""
Row -> :class:`BomLine` normalisation.

Takes the raw string grid plus a :class:`MappingResult` and produces clean,
typed BOM lines: quantities parsed, reference designators expanded, DNP flags
resolved, manufacturer names canonicalised, placeholder part numbers detected
and every original cell preserved in ``line.raw``.
"""

from __future__ import annotations

import re
from typing import Sequence

import re as _re

from ..core.models import BomLine, Severity
from ..util import log
from ..util.money import (
    CURRENCY_SYMBOLS, detect_decimal_comma, normalize_currency, parse_money,
)
from ..util.text import (
    clean, normalize_manufacturer, split_list_cell, split_refdes, tokens,
)
from ..util.units import (
    looks_dnp, looks_no_part, parse_quantity, normalize_package,
)
from .mapping import MappingResult

LOG = log.get("ingest.normalize")

# Values that mean "this cell is intentionally empty".
_PLACEHOLDER_CELLS = {
    "", "-", "--", "---", "n/a", "na", "none", "null", "nil", "tbd", "tba",
    "?", "??", "x", "xx", "#n/a", "#value!", "#ref!", "0", "no part",
    "not applicable", "see note", "see notes", "blank", "empty",
}

_NEGATIVE_FIT_WORDS = {"NO", "N", "FALSE", "0", "DNP", "DNI", "OMIT", "NOPOP",
                       "NOTFITTED", "UNPOPULATED", "OPEN", "NOTUSED"}
_POSITIVE_FIT_WORDS = {"YES", "Y", "TRUE", "1", "FIT", "FITTED", "POPULATE",
                       "POPULATED", "INSTALL", "INSTALLED", "LOAD", "LOADED",
                       "PLACE", "PLACED", "ASSEMBLE"}


def _cell(row: Sequence[str], index: int | None) -> str:
    if index is None or index < 0 or index >= len(row):
        return ""
    return clean(row[index])


def _is_placeholder(value: str) -> bool:
    return clean(value).lower() in _PLACEHOLDER_CELLS


_HEADER_CURRENCY_RE = _re.compile(
    r"[\(\[]?\b([A-Z]{3})\b[\)\]]?|([$€£¥₹])")


def _currency_from_header(header: str) -> str:
    """Pull a currency out of a price column header, e.g. ``Rate (EUR)``.

    Only three-letter codes that are real currencies count, so a header like
    ``Cost USD`` works while ``Cost EACH`` does not.
    """
    text = clean(header)
    if not text:
        return ""
    for match in _HEADER_CURRENCY_RE.finditer(text.upper()):
        code, symbol = match.group(1), match.group(2)
        if symbol:
            return normalize_currency(symbol, default="")
        if code and code in CURRENCY_SYMBOLS:
            return CURRENCY_SYMBOLS[code]
    return ""


def _resolve_dnp(raw_value: str, header: str, other_cells: Sequence[str]) -> bool:
    """Interpret a fit/populate column, which may be positive or negative sense.

    ``DNP``-named columns: a truthy value means do-not-populate.
    ``Populate``/``Fit``-named columns: a falsy value means do-not-populate.
    """
    value = clean(raw_value)
    joined = "".join(tokens(value))
    header_tokens = set(tokens(header))
    positive_sense = bool(
        header_tokens & {"POPULATE", "FIT", "FITTED", "INSTALL", "INSTALLED",
                         "LOAD", "LOADED", "ASSEMBLE", "PLACEMENT", "PLACE"}
    ) and not (header_tokens & {"DNP", "DNI", "NOT", "NO"})

    if value:
        if positive_sense:
            if joined in _NEGATIVE_FIT_WORDS:
                return True
            if joined in _POSITIVE_FIT_WORDS:
                return False
            return looks_dnp(value)
        if joined in _POSITIVE_FIT_WORDS or joined in {"DNP", "DNI", "DNF", "X"}:
            return True
        if joined in _NEGATIVE_FIT_WORDS - {"DNP", "DNI", "OMIT", "NOPOP",
                                            "NOTFITTED", "UNPOPULATED",
                                            "NOTUSED", "OPEN"}:
            return False
        return looks_dnp(value)
    # No dedicated column value: look everywhere else on the row.
    return looks_dnp(*other_cells)


def normalize_rows(rows: Sequence[tuple[int, Sequence[str]]],
                   mapping: MappingResult, *,
                   sheet_name: str = "",
                   expand_ref_ranges: bool = True,
                   treat_blank_qty_as_one: bool = True,
                   start_line_no: int = 1) -> list[BomLine]:
    """Convert data rows into :class:`BomLine` objects."""
    headers = mapping.headers
    get = mapping.mapping.get
    multi = mapping.multi_mapping
    lines: list[BomLine] = []
    line_no = start_line_no

    dnp_index = get("dnp")
    dnp_header = headers[dnp_index] if dnp_index is not None and \
        dnp_index < len(headers) else ""

    # Price columns are read with a column-wide decimal-separator decision and
    # a currency taken from the header when the cells do not carry one, so a
    # German "Rate (EUR)" column of "0,0032" values comes out correct.
    price_index = get("unit_price_in")
    decimal_comma = False
    header_currency = ""
    if price_index is not None:
        column = [_cell(row, price_index) for _, row in rows]
        decimal_comma = detect_decimal_comma(column)
        header_text = headers[price_index] if price_index < len(headers) else ""
        header_currency = _currency_from_header(header_text)

    # The quantity column gets the same treatment: in a German export
    # "1.000" is one thousand, and reading it as 1 is a 1000x error.
    qty_index = get("quantity")
    qty_decimal_comma = False
    if qty_index is not None:
        qty_column = [_cell(row, qty_index) for _, row in rows]
        qty_decimal_comma = detect_decimal_comma(qty_column) or decimal_comma

    for source_row, row in rows:
        cells = [clean(cell) for cell in row]
        if not any(cells):
            continue

        line = BomLine(line_no=line_no, source_row=source_row,
                       source_sheet=sheet_name)

        # Preserve everything, keyed by header (deduplicated).
        raw: dict[str, str] = {}
        for index, value in enumerate(cells):
            header = clean(headers[index]) if index < len(headers) else ""
            key = header or f"Column {index + 1}"
            if key in raw:
                suffix = 2
                while f"{key} ({suffix})" in raw:
                    suffix += 1
                key = f"{key} ({suffix})"
            raw[key] = value
        line.raw = raw

        # -- identifiers -------------------------------------------------- #
        mpn = _cell(cells, get("mpn"))
        # Some exports put the DNP marker in the part-number column; that is a
        # flag, not a part number, so it must not be sent to a provider.
        mpn_is_flag = bool(mpn) and looks_dnp(mpn)
        line.mpn = "" if (_is_placeholder(mpn) or mpn_is_flag) else mpn
        line.manufacturer = normalize_manufacturer(_cell(cells, get("manufacturer")))
        line.internal_pn = _cell(cells, get("internal_pn"))
        line.description = _cell(cells, get("description"))
        line.distributor = _cell(cells, get("distributor"))
        line.distributor_pn = _cell(cells, get("distributor_pn"))
        if _is_placeholder(line.distributor_pn):
            line.distributor_pn = ""

        # -- alternates (may span several columns) ------------------------ #
        alt_values: list[str] = []
        for index in multi.get("alt_mpns", []):
            alt_values.extend(split_list_cell(_cell(cells, index)))
        if line.mpn and "," in line.mpn:
            # Some tools put "MPN1, MPN2" in one cell.
            parts = split_list_cell(line.mpn)
            if len(parts) > 1 and all(len(p) >= 4 for p in parts):
                line.mpn = parts[0]
                alt_values.extend(parts[1:])
        seen_alt: set[str] = set()
        for value in alt_values:
            if _is_placeholder(value):
                continue
            key = value.upper()
            if key in seen_alt or key == line.mpn.upper():
                continue
            seen_alt.add(key)
            line.alt_mpns.append(value)

        # -- parametrics -------------------------------------------------- #
        line.value = _cell(cells, get("value"))
        line.package = _cell(cells, get("package"))
        line.tolerance = _cell(cells, get("tolerance"))
        line.voltage = _cell(cells, get("voltage"))
        line.power = _cell(cells, get("power"))
        line.notes = _cell(cells, get("notes"))
        line.level = _cell(cells, get("level"))
        line.parent = _cell(cells, get("parent"))

        # -- reference designators ---------------------------------------- #
        ref_cell = _cell(cells, get("ref_designators"))
        if ref_cell and not _is_placeholder(ref_cell):
            line.ref_designators = (
                split_refdes(ref_cell) if expand_ref_ranges
                else [p.strip() for p in re.split(r"[,;\n]+", ref_cell) if p.strip()]
            )

        # -- do-not-populate ---------------------------------------------- #
        scan_cells = [line.notes, line.description, ref_cell,
                      _cell(cells, get("line_id"))]
        if mpn_is_flag:
            scan_cells.append(mpn)
        line.dnp = _resolve_dnp(_cell(cells, dnp_index), dnp_header, scan_cells)

        # -- quantity ----------------------------------------------------- #
        qty_raw = _cell(cells, qty_index)
        quantity = parse_quantity(qty_raw, decimal_comma=qty_decimal_comma)
        if quantity is None:
            if line.ref_designators:
                quantity = float(len(line.ref_designators))
                line.add_issue(
                    "qty_from_refdes",
                    f"Quantity was blank; derived {int(quantity)} from the "
                    f"reference designators.",
                    Severity.INFO, field="quantity",
                )
            elif treat_blank_qty_as_one:
                quantity = 1.0
                if qty_raw:
                    line.add_issue("qty_unparsed",
                                   f"Could not read quantity {qty_raw!r}; "
                                   f"assumed 1.", Severity.WARNING,
                                   field="quantity")
                else:
                    line.add_issue("qty_blank",
                                   "Quantity was blank; assumed 1.",
                                   Severity.INFO, field="quantity")
            else:
                quantity = 0.0
                line.add_issue("qty_missing", "Quantity is missing.",
                               Severity.ERROR, field="quantity")
        line.quantity = quantity

        # -- price from file ---------------------------------------------- #
        price_raw = _cell(cells, price_index)
        if price_raw:
            amount, currency = parse_money(price_raw,
                                           decimal_comma=decimal_comma)
            line.unit_price_in = amount
            explicit_currency = _cell(cells, get("currency"))
            line.currency_in = normalize_currency(
                explicit_currency or currency or header_currency or "",
                default="")

        # -- placeholder / no-part detection ------------------------------ #
        if not line.mpn and not line.distributor_pn:
            if looks_no_part(line.description, line.notes, mpn):
                line.add_issue(
                    "no_part_marker",
                    "This line is marked as a placeholder rather than a real "
                    "part.", Severity.INFO, field="mpn")
            elif line.dnp:
                pass  # a DNP line with no part number is perfectly normal
            else:
                line.add_issue(
                    "mpn_missing",
                    "No manufacturer part number on this line.",
                    Severity.ERROR, field="mpn",
                    suggestion="Add an MPN, or let Schemata search by "
                               "description.")
        elif looks_no_part(line.mpn):
            line.add_issue("mpn_placeholder",
                           f"{line.mpn!r} is a placeholder, not a part number.",
                           Severity.WARNING, field="mpn")
            line.mpn = ""

        line.package = line.package  # kept verbatim; normalised on demand
        lines.append(line)
        line_no += 1

    return lines


def normalized_package(line: BomLine) -> str:
    """Canonical footprint for comparison purposes."""
    return normalize_package(line.package or line.value)


def summarise_lines(lines: Sequence[BomLine]) -> dict[str, object]:
    """Quick statistics used by the ingestion report."""
    with_mpn = sum(1 for line in lines if line.mpn)
    with_mfr = sum(1 for line in lines if line.manufacturer)
    with_refs = sum(1 for line in lines if line.ref_designators)
    dnp = sum(1 for line in lines if line.dnp)
    placements = sum(line.effective_quantity for line in lines)
    return {
        "lines": len(lines),
        "with_mpn": with_mpn,
        "with_manufacturer": with_mfr,
        "with_refdes": with_refs,
        "dnp_lines": dnp,
        "placements": int(placements),
        "mpn_coverage_pct": round(100.0 * with_mpn / len(lines), 1) if lines else 0.0,
    }
