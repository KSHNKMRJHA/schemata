"""
Engineering value parsing: quantities, component values, tolerances, ratings,
packages and lead times.

The parsers here are what let the engine understand that ``4k7``, ``4.7K``,
``4700R`` and ``4700 Ohm`` are the same resistor, and that ``0603`` and
``1608 Metric`` are the same footprint. They are used both for validation and
for alternate-part scoring.
"""

from __future__ import annotations

import math
import re
from typing import Iterable

from .text import clean, tokens

# --------------------------------------------------------------------------- #
# Quantities
# --------------------------------------------------------------------------- #

_QTY_CLEAN_RE = re.compile(r"[,\s'_]")
_QTY_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def parse_quantity(value: object, default: float | None = None,
                   decimal_comma: bool | None = None) -> float | None:
    """Parse a per-assembly quantity.

    Tolerates thousands separators, trailing units (``12 pcs``, ``3 ea``),
    parenthesised negatives and European decimal commas.

    ``decimal_comma`` states the convention of the *column* this cell came
    from. It matters: in a German export ``1.000`` is one thousand, and
    reading it as 1 understates the quantity by 1000x. Pass the result of
    :func:`bomiq.util.money.detect_decimal_comma` for the column.

    >>> parse_quantity("1,200 pcs")
    1200.0
    >>> parse_quantity("1.000", decimal_comma=True)
    1000.0
    >>> parse_quantity("1,5", decimal_comma=True)
    1.5
    """
    text = clean(value)
    if not text:
        return default
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = re.sub(r"(?i)\b(pcs?|pieces?|ea|each|nos?|qty|units?|off)\b", " ", text)
    text = text.replace("x", " ").replace("X", " ")
    body = text.strip()

    if decimal_comma:
        # Comma is the decimal mark, period groups thousands.
        if re.fullmatch(r"-?[\d.]+(,\d+)?", body):
            stripped = body.replace(".", "").replace(",", ".")
        else:
            stripped = _QTY_CLEAN_RE.sub("", text)
    else:
        stripped = _QTY_CLEAN_RE.sub("", text)
        # A lone comma with one or two trailing digits is a decimal comma even
        # without a column hint ("1,5" is never fifteen).
        if re.fullmatch(r"-?\d+,\d{1,2}", body):
            stripped = body.replace(",", ".")
    match = _QTY_NUM_RE.search(stripped)
    if not match:
        return default
    try:
        number = float(match.group(0))
    except ValueError:
        return default
    return -number if negative else number


def format_quantity(value: float | None) -> str:
    if value is None:
        return ""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


# --------------------------------------------------------------------------- #
# Do-not-populate detection
# --------------------------------------------------------------------------- #

_DNP_TOKENS = {
    "DNP", "DNI", "DNF", "DNNP", "NOPOP", "NOTPOPULATED", "NOPLACE",
    "DONOTPOPULATE", "DONOTINSTALL", "DONOTFIT", "DNM", "NOTFITTED", "NF",
    "OMIT", "OMITTED", "UNPOPULATED", "SPARE", "NOTUSED", "NU", "OPEN",
    "NOTLOADED", "DNL", "XOUT",
}

_NO_PART_TOKENS = {
    "NOPART", "TBD", "TOBEDETERMINED", "TBA", "PLACEHOLDER", "UNKNOWN",
    "SEEDRAWING", "REFONLY", "REFERENCEONLY", "MECHANICAL", "DOCUMENTATION",
    "ASSEMBLYONLY", "VIRTUAL", "NOBOM", "DONOTORDER",
}


def looks_dnp(*values: object) -> bool:
    """True when any of the given cells marks the line as do-not-populate."""
    for value in values:
        text = clean(value)
        if not text:
            continue
        joined = "".join(tokens(text))
        if joined in _DNP_TOKENS:
            return True
        for token in tokens(text):
            if token in _DNP_TOKENS:
                return True
        if re.search(r"(?i)\bdo\s*not\s*(populate|place|install|fit|load)\b", text):
            return True
    return False


def looks_no_part(*values: object) -> bool:
    """True when the cell is a placeholder rather than a real part number."""
    for value in values:
        joined = "".join(tokens(value))
        if joined and joined in _NO_PART_TOKENS:
            return True
    return False


# --------------------------------------------------------------------------- #
# Component values
# --------------------------------------------------------------------------- #

_SI_PREFIX = {
    "f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
    "m": 1e-3, "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "meg": 1e6, "MEG": 1e6,
    "G": 1e9, "g": 1e9, "T": 1e12, "R": 1.0, "r": 1.0, "E": 1.0,
}

_UNIT_KINDS = {
    "F": "capacitance", "FARAD": "capacitance", "FARADS": "capacitance",
    "OHM": "resistance", "OHMS": "resistance", "Ω": "resistance",
    "R": "resistance", "E": "resistance",
    "H": "inductance", "HENRY": "inductance", "HENRIES": "inductance",
    "HZ": "frequency", "HERTZ": "frequency",
    "V": "voltage", "VOLT": "voltage", "VOLTS": "voltage", "VDC": "voltage",
    "A": "current", "AMP": "current", "AMPS": "current", "AMPERE": "current",
    "W": "power", "WATT": "power", "WATTS": "power",
}

# value like 4k7 / 1R2 / 2u2 / 1n5
_RKM_RE = re.compile(
    r"^(\d+)\s*(R|E|K|M|G|T|p|n|u|µ|μ|m|f)\s*(\d+)$", re.IGNORECASE
)
# number followed by an optional prefix+unit tail, e.g. 4.7k / 100nF / 10 MHz
_VALUE_RE = re.compile(r"^(\d+(?:[\.,]\d+)?)\s*([A-Za-zµμΩ]{0,8})$")

# Longest first, so "OHMS" wins over "O" and "HZ" over "H".
_UNIT_SUFFIXES = sorted(_UNIT_KINDS, key=len, reverse=True)

# Prefix letters, resolved case-sensitively where the case carries meaning.
_PREFIX_EXACT = {
    "f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6,
    "μ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "G": 1e9, "g": 1e9,
    "T": 1e12, "t": 1e12, "R": 1.0, "r": 1.0, "E": 1.0, "e": 1.0,
    "P": 1e-12, "N": 1e-9, "U": 1e-6, "F": 1e-15,
    "MEG": 1e6, "meg": 1e6, "Meg": 1e6,
}

# Uppercase ``M`` is genuinely ambiguous in the wild. For resistance,
# frequency, power, voltage and current it means mega (10M = 10 megohm,
# 10MHZ = 10 megahertz). For capacitance and inductance a megafarad or
# megahenry is physically absurd, and legacy documentation uses "MF"/"MFD" for
# microfarad, so it resolves to micro there.
_M_BY_KIND = {
    "resistance": 1e6, "frequency": 1e6, "power": 1e6,
    "voltage": 1e6, "current": 1e6,
    "capacitance": 1e-6, "inductance": 1e-6,
}


def parse_value(value: object) -> tuple[float | None, str | None]:
    """Parse an engineering value into ``(base_si_value, kind)``.

    ``kind`` is one of resistance/capacitance/inductance/frequency/voltage/
    current/power, or ``None`` when the unit cannot be determined.

    >>> parse_value("4k7")
    (4700.0, 'resistance')
    >>> parse_value("100nF")
    (1e-07, 'capacitance')
    >>> parse_value("10UF")
    (1e-05, 'capacitance')
    >>> parse_value("2.2uH")
    (2.2e-06, 'inductance')
    >>> parse_value("10MHz")
    (10000000.0, 'frequency')
    """
    text = clean(value)
    if not text:
        return None, None
    text = text.replace("Ω", "Ω").replace("Ω", "Ω")
    text = re.sub(r"(?i)\b(nom|nominal|typ|typical|max|min)\b", "", text).strip()
    text = text.replace(" ", "")

    # Fractional power notation: "1/10W", "1/4 W".
    fraction = re.match(r"^(\d+)/(\d+)([A-Za-zΩ]{1,6})$", text)
    if fraction:
        top, bottom, tail = fraction.groups()
        if float(bottom) != 0:
            inner = parse_value(f"1{tail}")
            if inner[0] is not None:
                return inner[0] * (float(top) / float(bottom)), inner[1]

    rkm = _RKM_RE.match(text)
    if rkm:
        whole, prefix, frac = rkm.groups()
        kind = _rkm_kind(prefix)
        if prefix in ("M", "m"):
            multiplier = 1e-3 if prefix == "m" else _M_BY_KIND.get(kind or "",
                                                                   1e6)
        else:
            multiplier = _PREFIX_EXACT.get(
                prefix, _PREFIX_EXACT.get(prefix.lower(), 1.0))
        number = float(f"{whole}.{frac}")
        return number * multiplier, kind

    match = _VALUE_RE.match(text)
    if not match:
        return None, None
    number_text, tail = match.groups()
    number = float(number_text.replace(",", "."))
    if not tail:
        return number, None

    # Split the tail into prefix + unit by finding the longest known unit that
    # the tail ends with.
    upper = tail.upper()
    unit = ""
    kind = None
    for candidate in _UNIT_SUFFIXES:
        if upper.endswith(candidate):
            trimmed = upper[: len(upper) - len(candidate)]
            # A one-character tail such as "F" is the unit itself, never a
            # prefix, and "MF" must read as prefix+unit rather than unit alone.
            unit = candidate
            kind = _UNIT_KINDS[candidate]
            upper = trimmed
            tail = tail[: len(tail) - len(candidate)]
            break
    if kind is None and upper.endswith("S"):
        for candidate in _UNIT_SUFFIXES:
            if upper[:-1].endswith(candidate):
                unit = candidate
                kind = _UNIT_KINDS[candidate]
                tail = tail[: len(upper) - len(candidate) - 1]
                upper = upper[: len(upper) - len(candidate) - 1]
                break

    prefix = tail
    multiplier = 1.0
    if prefix:
        if prefix in ("M", "m"):
            multiplier = 1e-3 if prefix == "m" else _M_BY_KIND.get(kind or "",
                                                                   1e6)
        elif prefix in _PREFIX_EXACT:
            multiplier = _PREFIX_EXACT[prefix]
        elif prefix.lower() in _PREFIX_EXACT:
            multiplier = _PREFIX_EXACT[prefix.lower()]
        else:
            return None, None
        # A bare prefix with no unit: "10R"/"10E" is resistance by convention.
        if kind is None and prefix in ("R", "r", "E", "e"):
            kind = "resistance"
    _ = unit
    return number * multiplier, kind


def _rkm_kind(prefix: str) -> str | None:
    p = prefix.upper()
    if p in ("R", "E", "K", "M", "G", "T"):
        return "resistance"
    if prefix in ("p", "n", "u", "µ", "μ", "f"):
        return "capacitance"
    return None


_ENG_PREFIXES = [
    (1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""),
    (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p"), (1e-15, "f"),
]

_KIND_UNIT = {
    "resistance": "Ω", "capacitance": "F", "inductance": "H",
    "frequency": "Hz", "voltage": "V", "current": "A", "power": "W",
}


def format_value(base: float | None, kind: str | None = None) -> str:
    """Render an SI value with an engineering prefix.

    >>> format_value(4700.0, "resistance")
    '4.7 kΩ'
    """
    if base is None:
        return ""
    if base == 0:
        return f"0 {_KIND_UNIT.get(kind or '', '')}".strip()
    magnitude = abs(base)
    for scale, prefix in _ENG_PREFIXES:
        if magnitude >= scale * 0.999:
            number = base / scale
            text = f"{number:.6g}"
            return f"{text} {prefix}{_KIND_UNIT.get(kind or '', '')}".strip()
    return f"{base:g} {_KIND_UNIT.get(kind or '', '')}".strip()


def values_equivalent(a: object, b: object, rel_tol: float = 0.01) -> bool | None:
    """Compare two engineering values. ``None`` when not comparable."""
    va, ka = parse_value(a)
    vb, kb = parse_value(b)
    if va is None or vb is None:
        return None
    if ka and kb and ka != kb:
        return False
    if va == 0 or vb == 0:
        return va == vb
    return math.isclose(va, vb, rel_tol=rel_tol)


# --------------------------------------------------------------------------- #
# Tolerance
# --------------------------------------------------------------------------- #

_TOL_RE = re.compile(r"(?:±|\+/-|\+-)?\s*(\d+(?:\.\d+)?)\s*%")
_TOL_LETTER = {
    "B": 0.1, "C": 0.25, "D": 0.5, "F": 1.0, "G": 2.0, "J": 5.0,
    "K": 10.0, "M": 20.0, "Z": 30.0,
}


def parse_tolerance(value: object) -> float | None:
    """Return tolerance as a percentage (``1.0`` for +/-1%)."""
    text = clean(value)
    if not text:
        return None
    match = _TOL_RE.search(text)
    if match:
        return float(match.group(1))
    upper = text.upper().strip()
    if upper in _TOL_LETTER:
        return _TOL_LETTER[upper]
    return None


# --------------------------------------------------------------------------- #
# Packages / footprints
# --------------------------------------------------------------------------- #

# Imperial <-> metric chip package equivalence.
_CHIP_EQUIV = {
    "0201": "0603M", "0402": "1005M", "0603": "1608M", "0805": "2012M",
    "1206": "3216M", "1210": "3225M", "1812": "4532M", "2010": "5025M",
    "2512": "6332M", "01005": "0402M",
}
_METRIC_TO_IMPERIAL = {v[:-1]: k for k, v in _CHIP_EQUIV.items()}

_PACKAGE_ALIASES = {
    "SOT23": "SOT-23", "SOT233": "SOT-23-3", "SOT235": "SOT-23-5",
    "SOT236": "SOT-23-6", "SOT323": "SOT-323", "SOT89": "SOT-89",
    "SOT223": "SOT-223", "TSOT23": "TSOT-23",
    "SOIC8": "SOIC-8", "SO8": "SOIC-8", "SOP8": "SOIC-8",
    "SOIC14": "SOIC-14", "SOIC16": "SOIC-16",
    "TSSOP8": "TSSOP-8", "TSSOP14": "TSSOP-14", "TSSOP16": "TSSOP-16",
    "TSSOP20": "TSSOP-20", "MSOP8": "MSOP-8", "MSOP10": "MSOP-10",
    "QFN16": "QFN-16", "QFN20": "QFN-20", "QFN24": "QFN-24",
    "QFN32": "QFN-32", "QFN48": "QFN-48",
    "LQFP32": "LQFP-32", "LQFP48": "LQFP-48", "LQFP64": "LQFP-64",
    "LQFP100": "LQFP-100", "LQFP144": "LQFP-144",
    "TQFP32": "TQFP-32", "TQFP44": "TQFP-44", "TQFP64": "TQFP-64",
    "DIP8": "DIP-8", "PDIP8": "DIP-8", "DIP14": "DIP-14", "DIP16": "DIP-16",
    "TO220": "TO-220", "TO2203": "TO-220-3", "TO247": "TO-247",
    "TO252": "TO-252", "DPAK": "TO-252", "TO263": "TO-263", "D2PAK": "TO-263",
    "TO92": "TO-92", "SMA": "DO-214AC", "SMB": "DO-214AA", "SMC": "DO-214AB",
    "SOD123": "SOD-123", "SOD323": "SOD-323", "SOD523": "SOD-523",
    "BGA": "BGA", "UFBGA": "UFBGA", "WLCSP": "WLCSP",
}


def normalize_package(value: object) -> str:
    """Canonical footprint name.

    Strips EDA library prefixes, normalises chip sizes to the imperial code and
    puts a hyphen into the standard IC package names.

    >>> normalize_package("RESC1608X55N")
    '0603'
    >>> normalize_package("SOIC8")
    'SOIC-8'
    """
    text = clean(value).upper()
    if not text:
        return ""
    text = re.sub(r"^(FOOTPRINT|PKG|PACKAGE|LIB|SM|SMD|SMT)[_\-: ]+", "", text)
    text = text.replace("_", "-").strip("- ")

    # IPC-style names: RESC1608X55N, CAPC3216X180N, INDC2012X95
    ipc = re.match(r"^(RES|CAP|IND|DIO|LED)[CM]?(\d{4})[XN]?", text)
    if ipc:
        metric = ipc.group(2)
        if metric in _METRIC_TO_IMPERIAL:
            return _METRIC_TO_IMPERIAL[metric]

    bare = re.sub(r"[^A-Z0-9]", "", text)
    if bare in _CHIP_EQUIV:
        return bare
    if bare.endswith("M") and bare[:-1] in _METRIC_TO_IMPERIAL:
        return _METRIC_TO_IMPERIAL[bare[:-1]]
    if bare in _METRIC_TO_IMPERIAL:
        return _METRIC_TO_IMPERIAL[bare]
    if bare in _PACKAGE_ALIASES:
        return _PACKAGE_ALIASES[bare]

    # "0603 (1608 Metric)" or "1608 Metric" from distributor data.
    chip = re.match(r"^(\d{4,5})\b", text)
    if chip:
        code = chip.group(1)
        if code in _CHIP_EQUIV:
            return code
        if code in _METRIC_TO_IMPERIAL:
            return _METRIC_TO_IMPERIAL[code]
    return text


# Tokens that describe a mounting style rather than a specific footprint.
# "THT" == "THT" tells you nothing about whether two connectors interchange, so
# these compare as "unknown" rather than "same".
_GENERIC_PACKAGE_TOKENS = {
    "THT", "SMD", "SMT", "SMDSMT", "THROUGHHOLE", "SURFACEMOUNT", "MODULE",
    "SMDMODULE", "BULK", "RADIAL", "AXIAL", "PANEL", "CHASSIS", "BOARD",
    "MECHANICAL", "HARDWARE", "N/A", "NA", "OTHER", "CUSTOM", "STANDARD",
    "NONSTANDARD", "SMDPACKAGE", "PCB",
}


def packages_equivalent(a: object, b: object) -> bool | None:
    """Compare two footprints. ``None`` when they are not comparable.

    >>> packages_equivalent("0603", "1608 Metric")
    True
    >>> packages_equivalent("THT", "THT") is None
    True
    """
    na, nb = normalize_package(a), normalize_package(b)
    if not na or not nb:
        return None
    flat_a = re.sub(r"[^A-Z0-9]", "", na)
    flat_b = re.sub(r"[^A-Z0-9]", "", nb)
    if flat_a in _GENERIC_PACKAGE_TOKENS or flat_b in _GENERIC_PACKAGE_TOKENS:
        return None
    if na == nb:
        return True
    return None if (len(na) < 3 or len(nb) < 3) else False


def mount_type(package: object) -> str:
    """Best-effort ``SMD`` / ``THT`` classification of a footprint."""
    pkg = normalize_package(package)
    if not pkg:
        return ""
    tht = ("DIP-", "TO-92", "TO-220", "TO-247", "TO-3", "RADIAL", "AXIAL",
           "THT", "THROUGH")
    smd = ("SOIC", "TSSOP", "MSOP", "QFN", "QFP", "BGA", "CSP", "SOT", "SOD",
           "DO-214", "TO-252", "TO-263", "SON", "DFN")
    if pkg in _CHIP_EQUIV:
        return "SMD"
    for marker in tht:
        if marker in pkg:
            return "THT"
    for marker in smd:
        if marker in pkg:
            return "SMD"
    return ""


# --------------------------------------------------------------------------- #
# Lead time
# --------------------------------------------------------------------------- #

def parse_lead_time_days(value: object) -> int | None:
    """Parse a lead time expressed in weeks/days/months into days.

    >>> parse_lead_time_days("12 weeks")
    84
    >>> parse_lead_time_days("Stock")
    0
    """
    text = clean(value).lower()
    if not text:
        return None
    # "Out of stock" / "no stock" is the opposite of available, so the
    # negations have to be checked before the "stock" keyword.
    if any(phrase in text for phrase in
           ("out of stock", "no stock", "not in stock", "0 in stock",
            "backorder", "back order", "on order", "non-stock",
            "unavailable")):
        return None
    if any(word in text for word in ("stock", "immediate", "ready",
                                     "available now", "same day")):
        return 0
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to|–)?\s*(\d+(?:\.\d+)?)?\s*"
                      r"(day|days|d|week|weeks|wk|wks|w|month|months|mo|year|years)",
                      text)
    if not match:
        number = re.search(r"\d+", text)
        return int(number.group(0)) if number else None
    low = float(match.group(1))
    high = float(match.group(2)) if match.group(2) else low
    unit = match.group(3)
    value_avg = (low + high) / 2.0
    if unit.startswith("d"):
        factor = 1
    elif unit.startswith(("w",)):
        factor = 7
    elif unit.startswith("mo") or unit.startswith("month"):
        factor = 30
    elif unit.startswith("y"):
        factor = 365
    else:
        factor = 7
    return int(round(value_avg * factor))


def format_lead_time(days: int | None) -> str:
    if days is None:
        return ""
    if days <= 0:
        return "In stock"
    if days < 14:
        return f"{days} d"
    if days < 120:
        return f"{round(days / 7)} wk"
    return f"{days / 30:.0f} mo"


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #

def first_number(value: object) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", clean(value).replace(",", ""))
    return float(match.group(0)) if match else None


def parse_int(value: object, default: int | None = None) -> int | None:
    number = first_number(value)
    return int(number) if number is not None else default


def mean(values: Iterable[float]) -> float | None:
    items = [v for v in values if v is not None]
    return sum(items) / len(items) if items else None


def median(values: Iterable[float]) -> float | None:
    items = sorted(v for v in values if v is not None)
    if not items:
        return None
    mid = len(items) // 2
    if len(items) % 2:
        return items[mid]
    return (items[mid - 1] + items[mid]) / 2.0
