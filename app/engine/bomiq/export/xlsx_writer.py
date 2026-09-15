"""
A small, dependency-free XLSX writer.

Why not openpyxl/xlsxwriter: the report is a deliverable, so it must look
identical on every machine, including a packaged build with no site-packages.
This module writes the OOXML parts we actually need -- shared strings, styles
(fonts, fills, borders, number formats, alignment), column widths, frozen
panes, autofilter, merged cells and hyperlinks -- and nothing else.

It is deliberately write-only and streaming-friendly: rows are buffered per
sheet, then serialised once on save.

Usage
-----
::

    wb = Workbook()
    style = wb.style(bold=True, bg="1F2937", color="FFFFFF")
    sheet = wb.sheet("Summary", freeze="A2", widths=[40, 18])
    sheet.row(["Metric", "Value"], style=style)
    sheet.row(["Lines", 42])
    wb.save("report.xlsx")
"""

from __future__ import annotations

import datetime as _dt
import re
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence
from xml.sax.saxutils import escape, quoteattr

MAX_COL = 16384
_ILLEGAL_XML = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def col_letter(index: int) -> str:
    """0-based column index to spreadsheet letter (``0`` -> ``A``)."""
    if index < 0:
        raise ValueError("column index must be >= 0")
    name = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def cell_ref(row: int, col: int) -> str:
    """1-based row, 0-based column -> ``A1``."""
    return f"{col_letter(col)}{row}"


def _clean_xml(text: str) -> str:
    return _ILLEGAL_XML.sub("", text)


# --------------------------------------------------------------------------- #
# Styles
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Style:
    """An immutable cell format. Create these via :meth:`Workbook.style`."""

    bold: bool = False
    italic: bool = False
    size: float = 11.0
    color: str = ""                 # font colour, RRGGBB
    bg: str = ""                    # fill colour, RRGGBB
    number_format: str = ""
    align: str = ""                 # left | center | right
    valign: str = ""                # top | center | bottom
    wrap: bool = False
    border: str = ""                # "", "thin", "bottom", "top", "box"
    border_color: str = "D1D5DB"
    font: str = "Calibri"
    indent: int = 0

    def key(self) -> tuple:
        return (self.bold, self.italic, self.size, self.color, self.bg,
                self.number_format, self.align, self.valign, self.wrap,
                self.border, self.border_color, self.font, self.indent)


# Built-in number formats we rely on (ids below 164 are reserved by the spec).
_BUILTIN_FORMATS = {
    "General": 0, "0": 1, "0.00": 2, "#,##0": 3, "#,##0.00": 4,
    "0%": 9, "0.00%": 10, "yyyy-mm-dd": 14,
}


# --------------------------------------------------------------------------- #
# Sheets
# --------------------------------------------------------------------------- #

@dataclass
class _Cell:
    value: Any
    style_id: int = 0
    formula: str = ""
    hyperlink: str = ""


@dataclass
class Sheet:
    name: str
    workbook: "Workbook"
    rows: list[list[_Cell | None]] = field(default_factory=list)
    widths: dict[int, float] = field(default_factory=dict)
    freeze: str = ""
    autofilter: str = ""
    merges: list[str] = field(default_factory=list)
    hyperlinks: dict[str, str] = field(default_factory=dict)
    row_heights: dict[int, float] = field(default_factory=dict)
    hidden_columns: set[int] = field(default_factory=set)
    tab_color: str = ""

    # -- writing ---------------------------------------------------------- #

    def row(self, values: Sequence[Any], style: Style | int | None = None,
            styles: Sequence[Style | int | None] | None = None,
            height: float | None = None,
            hyperlinks: Sequence[str | None] | None = None) -> int:
        """Append a row. Returns its 1-based row number."""
        default_id = self.workbook.style_id(style) if style is not None else 0
        cells: list[_Cell | None] = []
        for index, value in enumerate(values):
            style_id = default_id
            if styles is not None and index < len(styles) and \
                    styles[index] is not None:
                style_id = self.workbook.style_id(styles[index])
            link = ""
            if hyperlinks is not None and index < len(hyperlinks):
                link = hyperlinks[index] or ""
            cells.append(_Cell(value=value, style_id=style_id, hyperlink=link))
        self.rows.append(cells)
        row_number = len(self.rows)
        if height:
            self.row_heights[row_number] = height
        for index, cell in enumerate(cells):
            if cell and cell.hyperlink:
                self.hyperlinks[cell_ref(row_number, index)] = cell.hyperlink
        return row_number

    def blank(self, count: int = 1) -> None:
        for _ in range(count):
            self.rows.append([])

    def set_widths(self, widths: Iterable[float | None]) -> None:
        for index, width in enumerate(widths):
            if width:
                self.widths[index] = float(width)

    def merge(self, first_row: int, first_col: int, last_row: int,
              last_col: int) -> None:
        self.merges.append(
            f"{cell_ref(first_row, first_col)}:{cell_ref(last_row, last_col)}")

    def set_autofilter(self, first_row: int, first_col: int, last_row: int,
                       last_col: int) -> None:
        self.autofilter = (f"{cell_ref(first_row, first_col)}:"
                           f"{cell_ref(last_row, last_col)}")

    @property
    def row_count(self) -> int:
        return len(self.rows)

    # -- serialisation ---------------------------------------------------- #

    def _dimension(self) -> str:
        width = max((len(row) for row in self.rows), default=1)
        return f"A1:{cell_ref(max(1, len(self.rows)), max(0, width - 1))}"

    def to_xml(self) -> str:
        parts: list[str] = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<worksheet xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships">',
            f'<dimension ref="{self._dimension()}"/>',
        ]
        if self.tab_color:
            parts.append(f'<sheetPr><tabColor rgb="FF{self.tab_color}"/>'
                         f'</sheetPr>')
        parts.append('<sheetViews><sheetView workbookViewId="0"')
        if self.freeze:
            parts.append('>')
            column, row = _split_ref(self.freeze)
            parts.append(
                f'<pane xSplit="{column}" ySplit="{row}" '
                f'topLeftCell="{self.freeze}" activePane="bottomRight" '
                f'state="frozen"/>')
            parts.append('</sheetView>')
        else:
            parts.append('/>')
        parts.append('</sheetViews>')
        parts.append('<sheetFormatPr defaultRowHeight="15"/>')

        if self.widths or self.hidden_columns:
            parts.append('<cols>')
            indices = sorted(set(self.widths) | self.hidden_columns)
            for index in indices:
                width = self.widths.get(index, 10.0)
                hidden = ' hidden="1"' if index in self.hidden_columns else ''
                parts.append(
                    f'<col min="{index + 1}" max="{index + 1}" '
                    f'width="{width:.2f}" customWidth="1"{hidden}/>')
            parts.append('</cols>')

        parts.append('<sheetData>')
        for row_index, cells in enumerate(self.rows, start=1):
            if not cells:
                continue
            height = self.row_heights.get(row_index)
            attributes = f' ht="{height:.2f}" customHeight="1"' if height else ''
            parts.append(f'<row r="{row_index}"{attributes}>')
            for col_index, cell in enumerate(cells):
                if cell is None:
                    continue
                parts.append(self._cell_xml(row_index, col_index, cell))
            parts.append('</row>')
        parts.append('</sheetData>')

        if self.autofilter:
            parts.append(f'<autoFilter ref="{self.autofilter}"/>')
        if self.merges:
            parts.append(f'<mergeCells count="{len(self.merges)}">')
            for merge in self.merges:
                parts.append(f'<mergeCell ref="{merge}"/>')
            parts.append('</mergeCells>')
        if self.hyperlinks:
            parts.append('<hyperlinks>')
            for index, (ref, _) in enumerate(sorted(self.hyperlinks.items()),
                                             start=1):
                parts.append(f'<hyperlink ref="{ref}" r:id="rId{index}"/>')
            parts.append('</hyperlinks>')
        parts.append('<pageMargins left="0.5" right="0.5" top="0.6" '
                     'bottom="0.6" header="0.3" footer="0.3"/>')
        parts.append('</worksheet>')
        return "".join(parts)

    def _cell_xml(self, row: int, col: int, cell: _Cell) -> str:
        ref = cell_ref(row, col)
        style = f' s="{cell.style_id}"' if cell.style_id else ''
        value = cell.value

        if cell.formula:
            return (f'<c r="{ref}"{style}>'
                    f'<f>{escape(_clean_xml(cell.formula))}</f></c>')
        if value is None or value == "":
            return f'<c r="{ref}"{style}/>' if cell.style_id else ''
        if isinstance(value, bool):
            return f'<c r="{ref}"{style} t="b"><v>{1 if value else 0}</v></c>'
        if isinstance(value, (int, float, Decimal)):
            if isinstance(value, float) and (value != value or
                                             value in (float("inf"),
                                                       float("-inf"))):
                return f'<c r="{ref}"{style}/>'
            return f'<c r="{ref}"{style}><v>{_num(value)}</v></c>'
        if isinstance(value, (_dt.datetime, _dt.date)):
            return (f'<c r="{ref}"{style}>'
                    f'<v>{_excel_serial(value)}</v></c>')
        index = self.workbook.string_id(str(value))
        return f'<c r="{ref}"{style} t="s"><v>{index}</v></c>'

    def rels_xml(self) -> str:
        if not self.hyperlinks:
            return ""
        parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                 '<Relationships xmlns="http://schemas.openxmlformats.org/'
                 'package/2006/relationships">']
        for index, (_, target) in enumerate(sorted(self.hyperlinks.items()),
                                            start=1):
            parts.append(
                f'<Relationship Id="rId{index}" Type="http://schemas.'
                f'openxmlformats.org/officeDocument/2006/relationships/'
                f'hyperlink" Target={quoteattr(target)} '
                f'TargetMode="External"/>')
        parts.append('</Relationships>')
        return "".join(parts)


def _split_ref(ref: str) -> tuple[int, int]:
    match = re.match(r"^([A-Z]+)(\d+)$", ref.upper())
    if not match:
        return 0, 1
    letters, digits = match.groups()
    column = 0
    for char in letters:
        column = column * 26 + (ord(char) - 64)
    return column - 1, int(digits) - 1


def _num(value: int | float | Decimal) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, int):
        return str(value)
    text = repr(float(value))
    return text


_EPOCH = _dt.datetime(1899, 12, 30)


def _excel_serial(value: _dt.datetime | _dt.date) -> str:
    if isinstance(value, _dt.datetime):
        delta = value.replace(tzinfo=None) - _EPOCH
        return f"{delta.days + delta.seconds / 86400.0:.10f}".rstrip("0")
    return str((value - _EPOCH.date()).days)


# --------------------------------------------------------------------------- #
# Workbook
# --------------------------------------------------------------------------- #

class Workbook:
    """A minimal but well-formed xlsx workbook."""

    def __init__(self) -> None:
        self.sheets: list[Sheet] = []
        self._strings: dict[str, int] = {}
        self._string_list: list[str] = []
        self._styles: dict[tuple, int] = {}
        self._style_list: list[Style] = []
        # style 0 is the default
        self.style_id(Style())

    # -- registration ----------------------------------------------------- #

    def sheet(self, name: str, freeze: str = "",
              widths: Sequence[float | None] | None = None,
              tab_color: str = "") -> Sheet:
        safe = _safe_sheet_name(name, {s.name for s in self.sheets})
        sheet = Sheet(name=safe, workbook=self, freeze=freeze,
                      tab_color=tab_color)
        if widths:
            sheet.set_widths(widths)
        self.sheets.append(sheet)
        return sheet

    def style(self, **kwargs: Any) -> Style:
        style = Style(**kwargs)
        self.style_id(style)
        return style

    def style_id(self, style: Style | int | None) -> int:
        if style is None:
            return 0
        if isinstance(style, int):
            return style
        key = style.key()
        existing = self._styles.get(key)
        if existing is not None:
            return existing
        self._style_list.append(style)
        index = len(self._style_list) - 1
        self._styles[key] = index
        return index

    def string_id(self, text: str) -> int:
        text = _clean_xml(text)
        existing = self._strings.get(text)
        if existing is not None:
            return existing
        self._string_list.append(text)
        index = len(self._string_list) - 1
        self._strings[text] = index
        return index

    # -- parts ------------------------------------------------------------ #

    def _shared_strings_xml(self) -> str:
        parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                 f'<sst xmlns="http://schemas.openxmlformats.org/'
                 f'spreadsheetml/2006/main" count="{len(self._string_list)}" '
                 f'uniqueCount="{len(self._string_list)}">']
        for text in self._string_list:
            space = ' xml:space="preserve"' if text != text.strip() else ''
            parts.append(f'<si><t{space}>{escape(text)}</t></si>')
        parts.append('</sst>')
        return "".join(parts)

    def _styles_xml(self) -> str:
        fonts: list[str] = []
        font_ids: dict[tuple, int] = {}
        fills = ['<fill><patternFill patternType="none"/></fill>',
                 '<fill><patternFill patternType="gray125"/></fill>']
        fill_ids: dict[str, int] = {}
        borders = ['<border><left/><right/><top/><bottom/><diagonal/></border>']
        border_ids: dict[tuple, int] = {}
        formats: list[tuple[int, str]] = []
        format_ids: dict[str, int] = dict(_BUILTIN_FORMATS)
        next_format_id = 164

        def font_id(style: Style) -> int:
            key = (style.bold, style.italic, style.size, style.color,
                   style.font)
            if key in font_ids:
                return font_ids[key]
            pieces = [f'<sz val="{style.size:g}"/>',
                      f'<name val="{escape(style.font)}"/>']
            if style.bold:
                pieces.insert(0, '<b/>')
            if style.italic:
                pieces.insert(0, '<i/>')
            if style.color:
                pieces.append(f'<color rgb="FF{style.color}"/>')
            fonts.append(f'<font>{"".join(pieces)}</font>')
            font_ids[key] = len(fonts) - 1
            return font_ids[key]

        def fill_id(style: Style) -> int:
            if not style.bg:
                return 0
            if style.bg in fill_ids:
                return fill_ids[style.bg]
            fills.append(f'<fill><patternFill patternType="solid">'
                         f'<fgColor rgb="FF{style.bg}"/>'
                         f'<bgColor indexed="64"/></patternFill></fill>')
            fill_ids[style.bg] = len(fills) - 1
            return fill_ids[style.bg]

        def border_id(style: Style) -> int:
            if not style.border:
                return 0
            key = (style.border, style.border_color)
            if key in border_ids:
                return border_ids[key]
            edge = f'<color rgb="FF{style.border_color}"/>'
            thin = f'thin'
            sides = {"left": "", "right": "", "top": "", "bottom": ""}
            if style.border in ("thin", "box"):
                sides = {name: thin for name in sides}
            elif style.border in sides:
                sides[style.border] = thin
            pieces = []
            for name in ("left", "right", "top", "bottom"):
                if sides[name]:
                    pieces.append(f'<{name} style="{sides[name]}">{edge}'
                                  f'</{name}>')
                else:
                    pieces.append(f'<{name}/>')
            borders.append(f'<border>{"".join(pieces)}<diagonal/></border>')
            border_ids[key] = len(borders) - 1
            return border_ids[key]

        def number_format_id(style: Style) -> int:
            nonlocal next_format_id
            code = style.number_format
            if not code:
                return 0
            if code in format_ids:
                return format_ids[code]
            format_ids[code] = next_format_id
            formats.append((next_format_id, code))
            next_format_id += 1
            return format_ids[code]

        xfs: list[str] = []
        for style in self._style_list:
            alignment = ""
            if style.align or style.valign or style.wrap or style.indent:
                attributes = []
                if style.align:
                    attributes.append(f'horizontal="{style.align}"')
                if style.valign:
                    attributes.append(f'vertical="{style.valign}"')
                if style.wrap:
                    attributes.append('wrapText="1"')
                if style.indent:
                    attributes.append(f'indent="{style.indent}"')
                alignment = f'<alignment {" ".join(attributes)}/>'
            apply_alignment = ' applyAlignment="1"' if alignment else ''
            xf = (f'<xf numFmtId="{number_format_id(style)}" '
                  f'fontId="{font_id(style)}" fillId="{fill_id(style)}" '
                  f'borderId="{border_id(style)}" xfId="0" '
                  f'applyFont="1" applyFill="1" applyBorder="1" '
                  f'applyNumberFormat="1"{apply_alignment}>'
                  f'{alignment}</xf>')
            xfs.append(xf)

        format_xml = ""
        if formats:
            entries = "".join(
                f'<numFmt numFmtId="{fid}" formatCode={quoteattr(code)}/>'
                for fid, code in formats)
            format_xml = f'<numFmts count="{len(formats)}">{entries}</numFmts>'

        return "".join([
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<styleSheet xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main">',
            format_xml,
            f'<fonts count="{len(fonts)}">{"".join(fonts)}</fonts>',
            f'<fills count="{len(fills)}">{"".join(fills)}</fills>',
            f'<borders count="{len(borders)}">{"".join(borders)}</borders>',
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" '
            'borderId="0"/></cellStyleXfs>',
            f'<cellXfs count="{len(xfs)}">{"".join(xfs)}</cellXfs>',
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" '
            'builtinId="0"/></cellStyles>',
            '</styleSheet>',
        ])

    def _workbook_xml(self) -> str:
        entries = "".join(
            f'<sheet name={quoteattr(sheet.name)} sheetId="{index + 1}" '
            f'r:id="rId{index + 1}"/>'
            for index, sheet in enumerate(self.sheets)
        )
        return "".join([
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<workbook xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.'
            'org/officeDocument/2006/relationships">',
            '<workbookPr date1904="false"/>',
            f'<sheets>{entries}</sheets>',
            '</workbook>',
        ])

    def _workbook_rels_xml(self) -> str:
        parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                 '<Relationships xmlns="http://schemas.openxmlformats.org/'
                 'package/2006/relationships">']
        for index in range(len(self.sheets)):
            parts.append(
                f'<Relationship Id="rId{index + 1}" Type="http://schemas.'
                f'openxmlformats.org/officeDocument/2006/relationships/'
                f'worksheet" Target="worksheets/sheet{index + 1}.xml"/>')
        base = len(self.sheets)
        parts.append(
            f'<Relationship Id="rId{base + 1}" Type="http://schemas.'
            f'openxmlformats.org/officeDocument/2006/relationships/styles" '
            f'Target="styles.xml"/>')
        parts.append(
            f'<Relationship Id="rId{base + 2}" Type="http://schemas.'
            f'openxmlformats.org/officeDocument/2006/relationships/'
            f'sharedStrings" Target="sharedStrings.xml"/>')
        parts.append('</Relationships>')
        return "".join(parts)

    def _content_types_xml(self) -> str:
        sheets = "".join(
            f'<Override PartName="/xl/worksheets/sheet{index + 1}.xml" '
            f'ContentType="application/vnd.openxmlformats-officedocument.'
            f'spreadsheetml.worksheet+xml"/>'
            for index in range(len(self.sheets))
        )
        return "".join([
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types">',
            '<Default Extension="rels" ContentType="application/vnd.'
            'openxmlformats-package.relationships+xml"/>',
            '<Default Extension="xml" ContentType="application/xml"/>',
            '<Override PartName="/xl/workbook.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
            sheets,
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
            '<Override PartName="/xl/sharedStrings.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml.'
            'sharedStrings+xml"/>',
            '</Types>',
        ])

    @staticmethod
    def _root_rels_xml() -> str:
        return "".join([
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
            '2006/relationships">',
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/>',
            '</Relationships>',
        ])

    # -- output ----------------------------------------------------------- #

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not self.sheets:
            self.sheet("Sheet1")
        # Serialise the sheets first: doing so registers shared strings and
        # styles, so both of those parts must be generated afterwards.
        serialised = [(sheet, sheet.to_xml(), sheet.rels_xml())
                      for sheet in self.sheets]
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", self._content_types_xml())
            archive.writestr("_rels/.rels", self._root_rels_xml())
            archive.writestr("xl/workbook.xml", self._workbook_xml())
            archive.writestr("xl/_rels/workbook.xml.rels",
                             self._workbook_rels_xml())
            for index, (_, sheet_xml, rels) in enumerate(serialised):
                archive.writestr(f"xl/worksheets/sheet{index + 1}.xml",
                                 sheet_xml)
                if rels:
                    archive.writestr(
                        f"xl/worksheets/_rels/sheet{index + 1}.xml.rels", rels)
            archive.writestr("xl/sharedStrings.xml", self._shared_strings_xml())
            archive.writestr("xl/styles.xml", self._styles_xml())
        return path


_INVALID_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")


def _safe_sheet_name(name: str, taken: set[str]) -> str:
    cleaned = _INVALID_SHEET_CHARS.sub("-", str(name or "Sheet")).strip("'")
    cleaned = cleaned[:31] or "Sheet"
    if cleaned not in taken:
        return cleaned
    for suffix in range(2, 100):
        candidate = f"{cleaned[:31 - len(str(suffix)) - 1]} {suffix}"
        if candidate not in taken:
            return candidate
    return cleaned[:28] + "999"
