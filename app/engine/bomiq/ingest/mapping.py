"""
Column mapping: decide which column of an arbitrary spreadsheet is the MPN,
which is the quantity, and so on.

Four independent signals are combined, strongest first:

1. **Saved template** -- the header fingerprint has been seen before.
2. **Synonym dictionary** -- ~500 header spellings harvested from Altium,
   OrCAD, KiCad, Mentor, Zuken, Solidworks, Arena, Agile, SAP, Odoo, Teamcenter,
   Valor, distributor quote sheets and hand-made Excel BOMs.
3. **Learned headers** -- anything the user has corrected before, weighted by
   how often they confirmed it.
4. **Content inference** -- what the column's own values look like (do they
   parse as quantities? do they look like reference designators? like MPNs?).

Every decision comes back with a confidence and a human-readable reason, so the
UI can show why a column was mapped and let the user override it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..util import log
from ..util.money import parse_money
from ..util.text import (
    best_ratio, clean, header_key, normalize_mpn, refdes_prefix, slug,
    split_refdes, tokens,
)
from ..util.units import parse_quantity, parse_value

LOG = log.get("ingest.mapping")


# --------------------------------------------------------------------------- #
# Canonical fields
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    synonyms: tuple[str, ...]
    patterns: tuple[str, ...] = ()
    required: bool = False
    multi: bool = False          # may map from several columns (alt MPNs)
    group: str = "core"
    description: str = ""


FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "mpn", "Manufacturer Part Number",
        synonyms=(
            "mpn", "manufacturer part number", "manufacturer part no",
            "manufacturer part #", "manufacturer pn", "manufacturer p/n",
            "mfr part number", "mfg part number", "mfr pn", "mfg pn",
            "mfr p/n", "mfg p/n", "mfrpartnumber", "manufacturerpartnumber",
            "part number", "part no", "part #", "partnumber", "part num",
            "pn", "p/n", "vendor part number", "supplier part number",
            "oem part number", "component part number", "manufacturer number",
            "mfr part", "mfg part", "manufacturer's part number",
            "manufacturerpartno", "mfr#", "mfg#", "mfr part no",
            "primary part number", "orderable part number", "device",
            "component pn", "manuf part nr", "part-number", "catalog number",
            "catalogue number", "mfr part number 1", "manufacturer part#",
            "manufacturer part number 1", "mpn1", "mpn 1",
            "part_number", "partno", "artikelnummer", "herstellernummer",
            "numero de piece", "hersteller teilenummer",
        ),
        patterns=(r"^m(fr|fg|anu)[a-z ]*part", r"part\s*(number|no|#|num)$",
                  r"^mpn\b"),
        required=True,
        description="The primary identifier used to look the part up.",
    ),
    FieldSpec(
        "manufacturer", "Manufacturer",
        synonyms=(
            "manufacturer", "mfr", "mfg", "mfr name", "mfg name",
            "manufacturer name", "make", "maker", "brand", "vendor",
            "supplier", "oem", "manuf", "manufac", "producer",
            "component manufacturer", "mfr.", "mfg.", "hersteller",
            "fabricant", "marca", "manufacturer 1", "mfr1",
        ),
        patterns=(r"^m(fr|fg|anufactur)", r"^(vendor|supplier|brand)$"),
        description="Used to disambiguate part numbers reused across vendors.",
    ),
    FieldSpec(
        "quantity", "Quantity",
        synonyms=(
            "qty", "quantity", "qnty", "qty.", "qty per board", "qty/board",
            "qty per assy", "qty per assembly", "quantity per assembly",
            "qty per unit", "qty pcs", "count", "no off", "number off",
            "nos", "pieces", "pcs", "usage", "used", "qty req",
            "quantity required", "req qty", "required quantity", "amount",
            "qty each", "each", "qty 1", "qtyper", "anzahl", "menge", "menge/me",
            "quantite", "cantidad", "qty (ea)", "board qty", "per board",
            "quantity/board", "line qty", "extended qty", "total qty",
        ),
        patterns=(r"^q(ty|nty|uantity)", r"\bqty\b", r"^no\.?\s*off$"),
        required=True,
        description="Per-assembly quantity. Blank is treated as 1.",
    ),
    FieldSpec(
        "ref_designators", "Reference Designators",
        synonyms=(
            "reference", "references", "refdes", "ref des", "ref-des",
            "ref designator", "reference designator", "reference designators",
            "designator", "designators", "ref", "refs", "location",
            "locations", "loc", "board location", "position", "positions",
            "part reference", "part references", "component reference",
            "schematic reference", "instance", "instances", "circuit ref",
            "bezeichnung", "referencia", "designation",
        ),
        patterns=(r"ref\s*des", r"^design(ator|ation)s?$", r"^ref(erence)?s?$"),
        description="Board positions; also used to sanity-check quantity.",
    ),
    FieldSpec(
        "description", "Description",
        synonyms=(
            "description", "desc", "descr", "part description",
            "component description", "item description", "long description",
            "short description", "name", "part name", "component name",
            "comment", "comments", "title", "details", "specification",
            "spec", "beschreibung", "descripcion", "libelle",
            "component type", "part type", "type",
        ),
        patterns=(r"^desc", r"description$"),
        description="Free text; used for parametric search when the MPN is "
                    "missing.",
    ),
    FieldSpec(
        "internal_pn", "Internal Part Number",
        synonyms=(
            "internal part number", "internal pn", "internal p/n", "ipn",
            "company part number", "house part number", "in house pn",
            "sku", "item", "item number", "item no", "item code",
            "material", "material number", "material no", "stock code",
            "stock number", "erp part number", "erp pn", "plm number",
            "our part number", "customer part number", "cpn",
            "internal number", "part id", "component id", "local pn",
            "artikel", "teilenummer", "code", "item id",
            # SAP / ERP export field names, which appear verbatim in extracts
            "matnr", "idnrk", "material no", "material code", "stock item",
            "inventory id", "oracle item", "item master",
        ),
        patterns=(r"^(internal|house|company|our|customer)\b.*\b(part|pn)",
                  r"^(ipn|cpn|sku)$", r"^material(\s*(number|no))?$",
                  r"^(matnr|idnrk)$"),
        description="Your own identifier; carried through to the report.",
    ),
    FieldSpec(
        "value", "Value",
        synonyms=(
            "value", "val", "component value", "nominal value", "rating",
            "resistance", "capacitance", "inductance", "frequency",
            "wert", "valor", "valeur",
        ),
        patterns=(r"^val(ue)?$",),
        description="Electrical value, checked against catalogue parametrics.",
    ),
    FieldSpec(
        "package", "Package / Footprint",
        synonyms=(
            "package", "pkg", "footprint", "case", "case code", "case size",
            "package type", "package/case", "package / case", "body",
            "outline", "land pattern", "pcb footprint", "smd package",
            "case/package", "size", "form factor", "gehause", "encapsulado",
            "boitier", "pattern", "library ref", "libref",
        ),
        patterns=(r"^(pkg|package|footprint|case)", r"package\s*/?\s*case"),
        description="Footprint; used to check alternates are drop-in.",
    ),
    FieldSpec(
        "tolerance", "Tolerance",
        synonyms=("tolerance", "tol", "tol.", "accuracy", "precision",
                  "toleranz", "tolerancia"),
        patterns=(r"^tol",),
    ),
    FieldSpec(
        "voltage", "Voltage Rating",
        synonyms=("voltage", "voltage rating", "rated voltage", "vdc", "wvdc",
                  "working voltage", "volt", "volts", "vmax", "v rating",
                  "spannung", "tension"),
        patterns=(r"^v(oltage)?(\s*rating)?$", r"^wvdc$"),
    ),
    FieldSpec(
        "power", "Power Rating",
        synonyms=("power", "power rating", "wattage", "watts", "rated power",
                  "dissipation", "leistung"),
        patterns=(r"^(power|wattage)",),
    ),
    FieldSpec(
        "alt_mpns", "Alternate Part Numbers",
        synonyms=(
            "alternate", "alternates", "alternate part number",
            "alternate part numbers", "alternative", "alternatives",
            "alt part number", "alt pn", "alt p/n", "alt mpn", "second source",
            "second source pn", "2nd source", "approved alternates",
            "substitute", "substitutes", "substitution", "aml",
            "approved manufacturer list", "avl", "approved vendor list",
            "equivalent", "equivalents", "cross reference", "cross ref",
            "mpn2", "mpn 2", "mpn3", "manufacturer part number 2",
            "mfr part number 2", "alternate manufacturer part number",
        ),
        patterns=(r"^alt", r"second\s*source", r"^mpn\s*[23456]$",
                  r"(part\s*number|pn)\s*[23456]$", r"^(aml|avl)$"),
        multi=True,
        description="Pre-approved substitutes, folded into alternate scoring.",
    ),
    FieldSpec(
        "distributor", "Distributor",
        synonyms=("distributor", "dist", "supplier name", "source",
                  "purchase from", "buy from", "seller", "reseller",
                  "distributor name", "vendor name", "lieferant"),
        patterns=(r"^dist", r"^supplier\s*name$"),
    ),
    FieldSpec(
        "distributor_pn", "Distributor Part Number",
        synonyms=(
            "distributor part number", "distributor pn", "distributor p/n",
            "dist pn", "dist part number", "digikey", "digi-key",
            "digikey part number", "digi-key part number", "digikey pn",
            "mouser", "mouser part number", "mouser pn", "farnell",
            "farnell part number", "farnell order code", "order code",
            "arrow part number", "rs part number", "rs stock no",
            "newark part number", "lcsc part number", "lcsc",
            "element14 code", "supplier part no", "supplier pn",
            "vendor part no", "dk part number", "dk#",
        ),
        patterns=(r"(digi\s*-?key|mouser|farnell|newark|element\s*14|arrow|"
                  r"lcsc|rs\s*components?)", r"order\s*code", r"^dist.*\bpn\b"),
        description="Distributor SKU; used as a fallback lookup key.",
    ),
    FieldSpec(
        "unit_price_in", "Unit Price (from file)",
        synonyms=(
            "price", "unit price", "unit cost", "cost", "cost each",
            "price each", "unit price usd", "target price", "last price",
            "standard cost", "std cost", "quoted price", "price/unit",
            "cost/unit", "preis", "precio", "prix", "unitprice",
            "material cost", "rate", "unit rate", "rate each", "basic rate",
            "list price", "net price", "buy price", "purchase price",
        ),
        patterns=(r"^(unit\s*)?(price|cost)", r"(price|cost)\s*(each|/?\s*unit)",
                  r"^rate\b", r"\b(price|cost|rate)\s*\([a-z]{3}\)$"),
        description="Your existing price, compared against live pricing.",
    ),
    FieldSpec(
        "currency", "Currency",
        synonyms=("currency", "curr", "ccy", "price currency", "waehrung"),
        patterns=(r"^(currency|ccy|curr)$",),
    ),
    FieldSpec(
        "notes", "Notes",
        synonyms=("notes", "note", "remark", "remarks", "comment", "comments",
                  "instructions", "assembly notes", "engineering notes",
                  "bemerkung", "observaciones"),
        patterns=(r"^(note|remark|comment)",),
    ),
    FieldSpec(
        "dnp", "Do Not Populate",
        synonyms=("dnp", "dni", "do not populate", "do not place",
                  "do not install", "populate", "install", "fitted",
                  "fit", "no populate", "nopop", "placement", "assemble",
                  "loaded", "load"),
        patterns=(r"^(dnp|dni|dnf)$", r"do\s*not\s*(populate|place|install|fit)",
                  r"^(populate|install|fit(ted)?|loaded?)$"),
        description="Lines flagged here are excluded from cost and stock checks.",
    ),
    FieldSpec(
        "level", "BOM Level",
        synonyms=("level", "bom level", "lvl", "indent", "indent level",
                  "tier", "depth", "structure", "ebene"),
        patterns=(r"^(bom\s*)?le?ve?l$", r"^indent"),
        description="Multi-level BOM indent, preserved in the report.",
    ),
    FieldSpec(
        "parent", "Parent Assembly",
        synonyms=("parent", "parent assembly", "parent pn", "assembly",
                  "assy", "top level", "used on", "where used", "sub assembly"),
        patterns=(r"^parent", r"^ass(y|embly)$"),
    ),
    FieldSpec(
        "line_id", "Line / Item Number",
        synonyms=("line", "line no", "line number", "line #", "item no",
                  "item number", "seq", "sequence", "sl no", "sl. no.",
                  "s no", "s.no", "sr no", "sr. no.", "serial", "index",
                  "#", "no", "no.", "pos", "position no", "row"),
        patterns=(r"^(sl|sr|s)\.?\s*no\.?$", r"^line(\s*(no|number|#))?$",
                  r"^(seq|sequence|index)$", r"^#$"),
        description="Row numbering in the source file; not used for analysis.",
    ),
)

FIELD_BY_KEY: dict[str, FieldSpec] = {spec.key: spec for spec in FIELD_SPECS}
FIELD_KEYS: tuple[str, ...] = tuple(spec.key for spec in FIELD_SPECS)
REQUIRED_FIELDS: tuple[str, ...] = tuple(
    spec.key for spec in FIELD_SPECS if spec.required)

# Pre-computed synonym index: normalised header -> (field, weight)
_SYNONYM_INDEX: dict[str, tuple[str, float]] = {}
for _spec in FIELD_SPECS:
    for _index, _synonym in enumerate(_spec.synonyms):
        _key = header_key(_synonym)
        if not _key:
            continue
        # Earlier synonyms are the more canonical spellings.
        _weight = 100.0 - min(_index, 30) * 0.4
        existing = _SYNONYM_INDEX.get(_key)
        if existing is None or existing[1] < _weight:
            _SYNONYM_INDEX[_key] = (_spec.key, _weight)


# --------------------------------------------------------------------------- #
# Content inference
# --------------------------------------------------------------------------- #

_REFDES_CELL_RE = re.compile(
    r"^[A-Za-z]{1,4}\d{1,5}([A-Za-z])?"
    r"([,;/\s]+[A-Za-z]{1,4}\d{1,5}([A-Za-z])?|\s*-\s*[A-Za-z]{0,4}\d{1,5})*$"
)
_MPN_CELL_RE = re.compile(r"^(?=.*\d)[A-Za-z0-9][A-Za-z0-9\-_./+#]{2,}$")
_CURRENCY_CELL_RE = re.compile(r"^[A-Z]{3}$")
_DNP_CELL_RE = re.compile(
    r"^(dnp|dni|dnf|yes|no|y|n|true|false|1|0|x|populate|do not populate|"
    r"install|fitted|not fitted|nopop)$", re.IGNORECASE)

# Designator letters that appear in real schematics. Used to tell a genuine
# reference-designator column from any other short alphanumeric code.
_COMMON_REFDES_PREFIXES = {
    "R", "C", "L", "U", "D", "Q", "J", "P", "X", "Y", "SW", "S", "F", "K",
    "T", "TP", "FB", "BT", "MH", "E", "ANT", "IC", "VR", "RN", "CN", "CON",
    "M", "W", "G", "LED", "Z", "CR", "TR", "XT", "OSC", "RL", "RY", "PB",
    "BAT", "HS", "MP", "MOD", "TB", "FL", "RV", "RT", "ZD", "TVS", "DS",
}

_KNOWN_MFR_HINTS = {
    "TEXASINSTRUMENTS", "TI", "STMICROELECTRONICS", "ST", "MURATA", "TDK",
    "YAGEO", "VISHAY", "PANASONIC", "KEMET", "SAMSUNG", "NXP", "MICROCHIP",
    "ANALOGDEVICES", "INFINEON", "ONSEMICONDUCTOR", "ONSEMI", "RENESAS",
    "TECONNECTIVITY", "MOLEX", "AMPHENOL", "BOURNS", "WURTH", "WURTHELEKTRONIK",
    "NICHICON", "ROHM", "TOSHIBA", "DIODES", "NEXPERIA", "LITTELFUSE",
    "ABRACON", "EPSON", "COILCRAFT", "OMRON", "AVX", "TAIYOYUDEN", "JST",
    "HIROSE", "BROADCOM", "MICRON", "WINBOND", "ESPRESSIF", "CREE", "OSRAM",
    "KINGBRIGHT", "HONEYWELL", "BOSCH", "NORDICSEMICONDUCTOR", "XILINX",
    "LATTICE", "SILICONLABS", "SKYWORKS", "MAXIM", "LINEARTECHNOLOGY",
}

_DISTRIBUTOR_HINTS = {
    "DIGIKEY", "DIGI-KEY", "MOUSER", "ARROW", "FARNELL", "ELEMENT14",
    "NEWARK", "RS", "RSCOMPONENTS", "LCSC", "AVNET", "TTI", "FUTURE",
    "HEILIND", "VERICAL", "ONLINECOMPONENTS", "BUERKLIN", "RUTRONIK",
}


def infer_from_values(values: Sequence[str]) -> dict[str, float]:
    """Score candidate fields from a column's own content (0..100 each)."""
    samples = [clean(v) for v in values if clean(v)]
    if not samples:
        return {}
    total = len(samples)
    scores: dict[str, float] = {}

    def fraction(predicate) -> float:
        return sum(1 for value in samples if predicate(value)) / total

    # quantity: small positive integers, low cardinality
    qty_like = fraction(lambda v: (parse_quantity(v) is not None
                                   and (parse_quantity(v) or 0) >= 0
                                   and len(v) <= 12))
    if qty_like > 0.85:
        numbers = [parse_quantity(v) or 0 for v in samples]
        integral = sum(1 for n in numbers if float(n).is_integer()) / total
        small = sum(1 for n in numbers if 0 <= n <= 5000) / total
        scores["quantity"] = 40 + 30 * integral + 25 * small
        # Long monotonic sequence starting at 1 -> row numbering, not quantity.
        if total >= 5:
            ints = [int(n) for n in numbers if float(n).is_integer()]
            if len(ints) == total and ints == list(range(ints[0], ints[0] + total)):
                scores["line_id"] = 88.0
                scores["quantity"] = 25.0

    # price
    money_like = fraction(lambda v: parse_money(v)[0] is not None)
    decimal_like = fraction(lambda v: "." in v or "," in v)
    symbol_like = fraction(lambda v: any(s in v for s in "$€£¥₹"))
    if money_like > 0.8 and (decimal_like > 0.5 or symbol_like > 0.2):
        scores["unit_price_in"] = 45 + 30 * decimal_like + 25 * symbol_like

    # reference designators. Multi-value cells and a spread of the usual
    # designator letters are what make this signal trustworthy; a lone "A12"
    # in a warehouse-bin column is not.
    refdes_like = fraction(lambda v: bool(_REFDES_CELL_RE.match(v)))
    multi_ref = fraction(lambda v: len(split_refdes(v)) > 1)
    if refdes_like > 0.6:
        prefixes = {refdes_prefix(part) for value in samples
                    for part in split_refdes(value)} - {""}
        common = prefixes & _COMMON_REFDES_PREFIXES
        confidence = 45 + 25 * refdes_like + 18 * multi_ref
        if common:
            confidence += 8 + min(9, 3 * len(common))
        scores["ref_designators"] = min(100.0, confidence)

    # manufacturer
    mfr_like = fraction(lambda v: slug(v) in _KNOWN_MFR_HINTS)
    if mfr_like > 0.25:
        scores["manufacturer"] = 50 + 50 * min(1.0, mfr_like * 1.5)

    # distributor
    dist_like = fraction(lambda v: slug(v) in _DISTRIBUTOR_HINTS)
    if dist_like > 0.3:
        scores["distributor"] = 55 + 45 * dist_like

    # MPN: mixed alphanumerics, high cardinality, not mostly words
    mpn_like = fraction(lambda v: bool(_MPN_CELL_RE.match(v)) and " " not in v)
    cardinality = len({v.upper() for v in samples}) / total
    if mpn_like > 0.55 and cardinality > 0.4:
        scores["mpn"] = 35 + 35 * mpn_like + 30 * cardinality

    # description: long, spaced, wordy
    wordy = fraction(lambda v: " " in v and len(v) > 12)
    if wordy > 0.6:
        scores["description"] = 40 + 50 * wordy

    # value: must parse *with a unit*. A column of bare numbers is a quantity,
    # a plant code or a line number -- never an engineering value.
    def _has_unit(text: str) -> bool:
        parsed, kind = parse_value(text)
        return parsed is not None and kind is not None

    value_like = fraction(lambda v: _has_unit(v) and len(v) <= 16)
    if value_like > 0.5:
        scores["value"] = 35 + 45 * value_like

    # currency
    if fraction(lambda v: bool(_CURRENCY_CELL_RE.match(v.upper()))) > 0.8:
        scores["currency"] = 90.0

    # dnp flags
    if fraction(lambda v: bool(_DNP_CELL_RE.match(v))) > 0.85 and \
            len({v.lower() for v in samples}) <= 4:
        scores["dnp"] = 70.0

    # tolerance
    if fraction(lambda v: "%" in v) > 0.7:
        scores["tolerance"] = 80.0

    return scores


# --------------------------------------------------------------------------- #
# Mapping result
# --------------------------------------------------------------------------- #

@dataclass
class ColumnCandidate:
    index: int
    header: str
    field: str
    confidence: float
    reason: str


@dataclass
class MappingResult:
    """Outcome of mapping a header row onto canonical fields."""

    # field key -> column index (or list of indices for multi fields)
    mapping: dict[str, int] = field(default_factory=dict)
    multi_mapping: dict[str, list[int]] = field(default_factory=dict)
    headers: list[str] = field(default_factory=list)
    confidence: dict[str, float] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    unmapped_indices: list[int] = field(default_factory=list)
    alternatives: dict[int, list[ColumnCandidate]] = field(default_factory=dict)
    fingerprint: str = ""
    template_id: str = ""
    template_name: str = ""
    from_template: bool = False

    @property
    def missing_required(self) -> list[str]:
        return [key for key in REQUIRED_FIELDS if key not in self.mapping]

    @property
    def overall_confidence(self) -> float:
        if not self.confidence:
            return 0.0
        weights = {"mpn": 3.0, "quantity": 2.0, "manufacturer": 1.5,
                   "description": 1.0, "ref_designators": 1.0}
        total = 0.0
        weight_sum = 0.0
        for key, value in self.confidence.items():
            weight = weights.get(key, 0.5)
            total += value * weight
            weight_sum += weight
        return round(total / weight_sum, 1) if weight_sum else 0.0

    def header_for(self, field_key: str) -> str:
        index = self.mapping.get(field_key)
        if index is None or index >= len(self.headers):
            return ""
        return self.headers[index]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mapping": dict(self.mapping),
            "multi_mapping": {k: list(v) for k, v in self.multi_mapping.items()},
            "headers": list(self.headers),
            "confidence": {k: round(v, 1) for k, v in self.confidence.items()},
            "reasons": dict(self.reasons),
            "unmapped_indices": list(self.unmapped_indices),
            "unmapped_headers": [
                self.headers[i] for i in self.unmapped_indices
                if i < len(self.headers)
            ],
            "alternatives": {
                str(index): [
                    {"field": c.field, "confidence": round(c.confidence, 1),
                     "reason": c.reason}
                    for c in candidates
                ]
                for index, candidates in self.alternatives.items()
            },
            "fingerprint": self.fingerprint,
            "template_id": self.template_id,
            "template_name": self.template_name,
            "from_template": self.from_template,
            "missing_required": self.missing_required,
            "overall_confidence": self.overall_confidence,
        }


def fingerprint_headers(headers: Iterable[str]) -> str:
    """Stable hash of a header row, insensitive to order and formatting."""
    keys = sorted({header_key(h) for h in headers if header_key(h)})
    return hashlib.sha1("|".join(keys).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# The mapper
# --------------------------------------------------------------------------- #

class ColumnMapper:
    """Maps a header row (plus sample data) onto canonical field keys."""

    #: minimum confidence to accept an automatic mapping
    ACCEPT_THRESHOLD = 55.0
    #: below this the UI asks the user to confirm
    REVIEW_THRESHOLD = 72.0

    def __init__(self, template_store: Any | None = None,
                 fuzzy_cutoff: float = 82.0) -> None:
        self.templates = template_store
        self.fuzzy_cutoff = fuzzy_cutoff
        self._learned: dict[str, tuple[str, int]] = {}
        if template_store is not None:
            try:
                self._learned = template_store.learned_map()
            except Exception as exc:  # pragma: no cover
                LOG.debug("Could not load learned headers: %s", exc)

    # -- single header scoring -------------------------------------------- #

    def score_header(self, header: str) -> list[tuple[str, float, str]]:
        """Candidate ``(field, confidence, reason)`` list for one header."""
        key = header_key(header)
        text = clean(header)
        if not key:
            return []
        out: list[tuple[str, float, str]] = []

        exact = _SYNONYM_INDEX.get(key)
        if exact:
            out.append((exact[0], exact[1], f"header matches '{text}'"))

        learned = self._learned.get(key)
        if learned:
            field_key, hits = learned
            confidence = min(99.0, 84.0 + min(hits, 15))
            out.append((field_key, confidence,
                        f"learned from {hits} previous confirmation"
                        f"{'s' if hits != 1 else ''}"))

        lowered = key
        for spec in FIELD_SPECS:
            for pattern in spec.patterns:
                if re.search(pattern, lowered) or re.search(pattern, text.lower()):
                    out.append((spec.key, 78.0, f"header pattern '{pattern}'"))
                    break

        # Fuzzy against all synonyms (only if nothing strong yet).
        if not out or max(score for _, score, _ in out) < 80:
            best_field, best_score, best_synonym = None, 0.0, ""
            for spec in FIELD_SPECS:
                for synonym in spec.synonyms[:24]:
                    score = best_ratio(key, header_key(synonym))
                    if score > best_score:
                        best_field, best_score, best_synonym = (
                            spec.key, score, synonym)
            if best_field and best_score >= self.fuzzy_cutoff:
                out.append((best_field, best_score * 0.9,
                            f"similar to '{best_synonym}' ({best_score:.0f}%)"))

        # Token containment, e.g. "Mfr Part Number (primary)"
        header_tokens = set(tokens(header))
        if header_tokens:
            for spec in FIELD_SPECS:
                for synonym in spec.synonyms[:12]:
                    synonym_tokens = set(tokens(synonym))
                    if len(synonym_tokens) >= 2 and synonym_tokens <= header_tokens:
                        out.append((spec.key, 80.0,
                                    f"header contains '{synonym}'"))
                        break

        # Collapse to the best score per field.
        best: dict[str, tuple[float, str]] = {}
        for field_key, score, reason in out:
            current = best.get(field_key)
            if current is None or current[0] < score:
                best[field_key] = (score, reason)
        return sorted(
            ((k, v[0], v[1]) for k, v in best.items()),
            key=lambda item: -item[1],
        )

    # -- full row mapping -------------------------------------------------- #

    def map_columns(self, headers: Sequence[str],
                    samples: Sequence[Sequence[str]] | None = None,
                    forced: dict[str, int] | None = None) -> MappingResult:
        """Map a header row, using column content as a tie-breaker.

        ``samples`` is a list of columns (each a list of that column's values).
        ``forced`` pins specific fields to specific column indices.
        """
        headers = [clean(h) for h in headers]
        result = MappingResult(headers=list(headers))
        result.fingerprint = fingerprint_headers(headers)

        # 1) template replay
        if self.templates is not None:
            try:
                template = self.templates.find(result.fingerprint)
            except Exception:  # pragma: no cover
                template = None
            if template:
                mapping = {
                    key: index for key, index in template["mapping"].items()
                    if isinstance(index, int) and 0 <= index < len(headers)
                    and key in FIELD_BY_KEY
                }
                if mapping:
                    result.mapping = mapping
                    result.confidence = {key: 99.0 for key in mapping}
                    result.reasons = {
                        key: f"saved template '{template['name']}'"
                        for key in mapping
                    }
                    result.template_id = template["id"]
                    result.template_name = template["name"]
                    result.from_template = True

        # 2) score every column against every field
        header_scores: list[list[tuple[str, float, str]]] = [
            self.score_header(header) for header in headers
        ]
        content_scores: list[dict[str, float]] = []
        for index in range(len(headers)):
            column = list(samples[index]) if samples and index < len(samples) else []
            content_scores.append(infer_from_values(column) if column else {})

        # combine: header evidence dominates, content adjusts
        combined: dict[int, dict[str, tuple[float, str]]] = {}
        for index, candidates in enumerate(header_scores):
            per_field: dict[str, tuple[float, str]] = {}
            for field_key, score, reason in candidates:
                content = content_scores[index].get(field_key, 0.0)
                if content >= 50:
                    score = min(100.0, score + 8.0)
                    reason = f"{reason}; values look like {field_key}"
                elif content_scores[index] and field_key in (
                        "quantity", "unit_price_in", "ref_designators",
                        "currency", "dnp"):
                    # These are strongly content-determined; a contradiction is
                    # meaningful.
                    score -= 12.0
                    reason = f"{reason}; values do not look like {field_key}"
                per_field[field_key] = (score, reason)
            # Content-only inference. The bar depends on whether the column
            # has a name at all: a column called "Warehouse Bin" whose values
            # happen to look like designators must not be hijacked, while a
            # blank header leaves nothing but the values to go on.
            has_header = bool(clean(headers[index]) if index < len(headers)
                              else "")
            sample_count = len(samples[index]) if samples and \
                index < len(samples) else 0
            threshold = 88.0 if has_header else 70.0
            for field_key, content in content_scores[index].items():
                if field_key in per_field:
                    continue
                if content < threshold or sample_count < 2:
                    continue
                per_field[field_key] = (
                    content * (0.72 if has_header else 0.8),
                    f"values look like "
                    f"{FIELD_BY_KEY[field_key].label.lower()}"
                    f"{'' if has_header else ' (column has no header)'}",
                )
            combined[index] = per_field

        # 3) greedy assignment, best score first, one column per field
        proposals: list[tuple[float, int, str, str]] = []
        for index, per_field in combined.items():
            for field_key, (score, reason) in per_field.items():
                proposals.append((score, index, field_key, reason))
        proposals.sort(key=lambda item: (-item[0], item[1]))

        taken_columns: set[int] = set(result.mapping.values())
        taken_fields: set[str] = set(result.mapping)
        multi: dict[str, list[int]] = {}

        for score, index, field_key, reason in proposals:
            if score < self.ACCEPT_THRESHOLD:
                continue
            spec = FIELD_BY_KEY[field_key]
            if index in taken_columns:
                continue
            if spec.multi:
                multi.setdefault(field_key, []).append(index)
                taken_columns.add(index)
                if field_key not in result.confidence:
                    result.confidence[field_key] = score
                    result.reasons[field_key] = reason
                continue
            if field_key in taken_fields:
                continue
            result.mapping[field_key] = index
            result.confidence[field_key] = score
            result.reasons[field_key] = reason
            taken_fields.add(field_key)
            taken_columns.add(index)

        # 4) forced overrides win outright
        for field_key, index in (forced or {}).items():
            if field_key not in FIELD_BY_KEY:
                continue
            if index is None or index < 0:
                result.mapping.pop(field_key, None)
                result.confidence.pop(field_key, None)
                continue
            for other_field, other_index in list(result.mapping.items()):
                if other_index == index and other_field != field_key:
                    del result.mapping[other_field]
                    result.confidence.pop(other_field, None)
            result.mapping[field_key] = int(index)
            result.confidence[field_key] = 100.0
            result.reasons[field_key] = "set manually"

        result.multi_mapping = {
            key: sorted(set(indices)) for key, indices in multi.items()
        }
        assigned = set(result.mapping.values())
        for indices in result.multi_mapping.values():
            assigned.update(indices)
        result.unmapped_indices = [
            index for index in range(len(headers)) if index not in assigned
        ]
        result.alternatives = {
            index: [
                ColumnCandidate(index, headers[index], field_key, score, reason)
                for field_key, (score, reason) in sorted(
                    combined.get(index, {}).items(), key=lambda kv: -kv[1][0])[:4]
            ]
            for index in range(len(headers))
        }
        return result

    # -- learning --------------------------------------------------------- #

    def remember(self, result: MappingResult, name: str = "",
                 save_template: bool = True) -> str:
        """Persist a confirmed mapping as a template and header learnings."""
        if self.templates is None:
            return ""
        for field_key, index in result.mapping.items():
            if 0 <= index < len(result.headers):
                key = header_key(result.headers[index])
                if key:
                    self.templates.learn_header(key, field_key)
        for field_key, indices in result.multi_mapping.items():
            for index in indices:
                if 0 <= index < len(result.headers):
                    key = header_key(result.headers[index])
                    if key:
                        self.templates.learn_header(key, field_key)
        self._learned = self.templates.learned_map()
        if not save_template:
            return ""
        template_name = name or _suggest_template_name(result)
        return self.templates.save(
            template_name, result.fingerprint, dict(result.mapping),
            {"multi_mapping": result.multi_mapping},
        )


def _suggest_template_name(result: MappingResult) -> str:
    hints = [
        result.header_for("mpn"), result.header_for("quantity"),
        result.header_for("ref_designators"),
    ]
    label = " / ".join(h for h in hints if h)
    return f"{label or 'BOM'} ({len(result.headers)} cols)"


def describe_fields() -> list[dict[str, Any]]:
    """Field catalogue for the UI's mapping editor."""
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "required": spec.required,
            "multi": spec.multi,
            "description": spec.description,
            "examples": list(spec.synonyms[:6]),
        }
        for spec in FIELD_SPECS
    ]


def normalize_mpn_for_key(value: str) -> str:
    """Re-exported so callers do not need to import two modules."""
    return normalize_mpn(value)
