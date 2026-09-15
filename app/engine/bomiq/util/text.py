"""
Text normalisation, tokenisation and similarity primitives.

Everything here is pure-stdlib and deterministic. ``rapidfuzz`` is used when it
is installed because it is ~50x faster, but the stdlib ``difflib`` path returns
scores within a few points of it, so results stay stable either way.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable, Sequence

try:  # pragma: no cover - optional accelerator
    from rapidfuzz import fuzz as _rf_fuzz

    _HAVE_RAPIDFUZZ = True
except Exception:  # pragma: no cover
    _rf_fuzz = None
    _HAVE_RAPIDFUZZ = False


# --------------------------------------------------------------------------- #
# Basic cleaning
# --------------------------------------------------------------------------- #

_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")
_ZERO_WIDTH = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0xFEFF, 0x00AD, 0x2060], None
)
_DASHES = {
    0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2013: "-", 0x2014: "-",
    0x2015: "-", 0x2212: "-", 0xFF0D: "-",
}
_QUOTES = {
    0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201B: "'",
    0x201C: '"', 0x201D: '"', 0x201E: '"', 0x00B4: "'", 0x02BC: "'",
}
_TRANSLATE = {**_ZERO_WIDTH, **_DASHES, **_QUOTES}


def clean(value: object) -> str:
    """Normalise arbitrary cell content into a tidy single-line string.

    Handles unicode dashes/quotes, zero-width characters, NBSP, control
    characters, embedded newlines and redundant whitespace. ``None`` and the
    common spreadsheet null tokens collapse to ``""``.
    """
    if value is None:
        return ""
    if isinstance(value, float):
        # Avoid '10.0' for integral floats coming out of spreadsheets.
        if value != value:  # NaN
            return ""
        if value in (float("inf"), float("-inf")):
            return ""
        if float(value).is_integer():
            value = int(value)
    text = str(value)
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_TRANSLATE)
    text = text.replace(" ", " ")
    text = "".join(ch if ch >= " " or ch == "\t" else " " for ch in text)
    text = _WS_RE.sub(" ", text).strip()
    if text.lower() in {"nan", "none", "null", "n/a", "na", "#n/a", "-", "--",
                        "#value!", "#ref!", "#name?", "(blank)", "<blank>"}:
        return ""
    return text


def slug(value: object) -> str:
    """Aggressive key form: uppercase, alphanumerics only. Used for lookups."""
    return _NON_ALNUM_RE.sub("", clean(value).upper())


def header_key(value: object) -> str:
    """Canonical key for a column header (lowercase, alnum only)."""
    return _NON_ALNUM_RE.sub("", clean(value).upper()).lower()


def tokens(value: object) -> list[str]:
    """Split text into comparable uppercase alphanumeric tokens."""
    return [t for t in _NON_ALNUM_RE.split(clean(value).upper()) if t]


def is_blank(value: object) -> bool:
    return clean(value) == ""


# --------------------------------------------------------------------------- #
# Manufacturer part number normalisation
# --------------------------------------------------------------------------- #

# Suffixes distributors and EDA tools bolt on that do not change the part.
_PACKAGING_SUFFIXES = (
    "-ND", "-TR", "-TR13", "-TRPBF", "-T", "-TA", "-TB", "-TE", "-TL",
    "-CT", "-DKR", "-2", "-1", "-REEL", "-REEL7", "-BULK", "-TUBE",
    "-TRAY", "-PBFREE", "-PBF", "-CUT", "-RL", "-RL7", "-A", "-AMMO",
    "/TR", "/T", "/TR13", "/REEL", "/BULK", "/CUT", "#PBF", "#TR",
)


def normalize_mpn(mpn: object) -> str:
    """Canonical comparison form of an MPN.

    Uppercases, strips whitespace and punctuation that carries no meaning, and
    keeps the alphanumeric spine. This is intentionally lossy -- it is a
    *bucketing* key, never a value shown to the user or sent to an API.
    """
    text = clean(mpn).upper()
    if not text:
        return ""
    # Drop trailing revision / packaging noise in parentheses or brackets.
    text = re.sub(r"[\(\[\{].*?[\)\]\}]", "", text)
    text = text.strip()
    return _NON_ALNUM_RE.sub("", text)


def mpn_root(mpn: object) -> str:
    """MPN with known packaging suffixes removed, still human readable."""
    text = clean(mpn).upper()
    if not text:
        return ""
    changed = True
    while changed:
        changed = False
        for suffix in _PACKAGING_SUFFIXES:
            if len(text) > len(suffix) + 3 and text.endswith(suffix):
                text = text[: -len(suffix)]
                changed = True
    return text.strip("-/_ .#")


# --------------------------------------------------------------------------- #
# Manufacturer name normalisation
# --------------------------------------------------------------------------- #

_MFR_NOISE = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD",
    "LIMITED", "LLC", "PLC", "GMBH", "AG", "SA", "SAS", "NV", "BV", "AB",
    "OY", "KK", "PTE", "PVT", "PRIVATE", "GROUP", "HOLDINGS", "THE",
    "TECHNOLOGY", "TECHNOLOGIES", "SEMICONDUCTOR", "SEMICONDUCTORS",
    "ELECTRONIC", "ELECTRONICS", "COMPONENTS", "INDUSTRIES", "INTERNATIONAL",
    "MICROELECTRONICS", "PRODUCTS", "DEVICES", "SOLUTIONS", "AMERICA",
    "USA", "EUROPE", "JAPAN", "KOREA", "TAIWAN", "CHINA", "AND",
}

# Canonical names for the manufacturers whose aliases appear constantly in
# real BOMs. Key is the aggressive slug of an alias.
MFR_ALIASES: dict[str, str] = {}


def _register_aliases(canonical: str, *aliases: str) -> None:
    for alias in (canonical,) + aliases:
        MFR_ALIASES[slug(alias)] = canonical


_register_aliases("Texas Instruments", "TI", "Texas Instrument", "Texas Inst",
                  "Burr-Brown", "National Semiconductor", "NatSemi", "TI Inc")
_register_aliases("STMicroelectronics", "ST", "ST Micro", "ST Microelectronics",
                  "SGS-Thomson", "STM")
_register_aliases("Analog Devices", "ADI", "Analog Devices Inc",
                  "Linear Technology", "Linear Tech", "LTC", "Maxim Integrated",
                  "Maxim", "Hittite")
_register_aliases("Microchip Technology", "Microchip", "Atmel", "Micrel",
                  "SST", "Microsemi")
_register_aliases("NXP Semiconductors", "NXP", "Freescale", "Freescale Semiconductor",
                  "Philips Semiconductors")
_register_aliases("Infineon Technologies", "Infineon", "International Rectifier",
                  "IR", "Cypress", "Cypress Semiconductor", "Ramtron")
_register_aliases("ON Semiconductor", "ON Semi", "Onsemi", "Fairchild",
                  "Fairchild Semiconductor", "AMI Semiconductor")
_register_aliases("Renesas Electronics", "Renesas", "Intersil", "IDT",
                  "Integrated Device Technology", "Dialog Semiconductor",
                  "Dialog", "NEC Electronics")
_register_aliases("Murata Electronics", "Murata", "Murata Manufacturing",
                  "Murata Mfg", "Toko")
_register_aliases("TDK Corporation", "TDK", "TDK-EPC", "EPCOS", "TDK Epcos",
                  "InvenSense")
_register_aliases("Samsung Electro-Mechanics", "Samsung", "SEMCO",
                  "Samsung Electro Mechanics")
_register_aliases("Yageo", "Yageo Corporation", "Phycomp", "KEMET", "Kemet",
                  "NIC Components")
_register_aliases("Vishay", "Vishay Intertechnology", "Vishay Dale",
                  "Vishay Sprague", "Vishay Beyschlag", "Dale", "Sprague",
                  "Siliconix", "Vishay Siliconix")
_register_aliases("Panasonic", "Panasonic Electronic Components", "Matsushita",
                  "Panasonic Industrial")
_register_aliases("Nichicon", "Nichicon Corporation")
_register_aliases("Rubycon", "Rubycon Corporation")
_register_aliases("Wurth Elektronik", "Wurth", "Würth", "Würth Elektronik",
                  "WE", "Wurth Electronics")
_register_aliases("Bourns", "Bourns Inc")
_register_aliases("TE Connectivity", "TE", "Tyco Electronics", "Tyco",
                  "AMP", "Raychem", "Deutsch")
_register_aliases("Molex", "Molex Connector", "Molex LLC")
_register_aliases("Amphenol", "Amphenol ICC", "FCI", "Amphenol Commercial")
_register_aliases("JST", "JST Sales", "Japan Solderless Terminals")
_register_aliases("Hirose Electric", "Hirose", "HRS")
_register_aliases("Rohm Semiconductor", "Rohm", "Lapis", "Lapis Semiconductor")
_register_aliases("Toshiba", "Toshiba Semiconductor")
_register_aliases("Diodes Incorporated", "Diodes Inc", "Diodes", "Zetex",
                  "Pericom")
_register_aliases("Nexperia", "Nexperia BV")
_register_aliases("Skyworks Solutions", "Skyworks", "Silicon Labs",
                  "Silicon Laboratories", "SiLabs")
_register_aliases("Qualcomm", "Qualcomm Atheros", "CSR")
_register_aliases("Broadcom", "Avago", "Avago Technologies", "LSI")
_register_aliases("Micron Technology", "Micron", "Numonyx")
_register_aliases("Winbond Electronics", "Winbond", "Nuvoton")
_register_aliases("Espressif Systems", "Espressif", "Espressif Inc")
_register_aliases("Littelfuse", "Littelfuse Inc", "IXYS", "Teccor")
_register_aliases("Bel Fuse", "Bel", "Cinch", "Cinch Connectivity")
_register_aliases("Abracon", "Abracon LLC", "Abracon Corporation")
_register_aliases("Epson Timing", "Epson", "Seiko Epson")
_register_aliases("NDK", "Nihon Dempa Kogyo")
_register_aliases("Coilcraft", "Coilcraft Inc")
_register_aliases("Pulse Electronics", "Pulse", "Pulse Engineering")
_register_aliases("Laird", "Laird Technologies", "Laird Performance Materials")
_register_aliases("Omron", "Omron Electronics", "Omron Automation")
_register_aliases("Alps Alpine", "Alps", "Alps Electric")
_register_aliases("CTS Corporation", "CTS", "CTS Electronic Components")
_register_aliases("Stackpole Electronics", "Stackpole", "SEI")
_register_aliases("Susumu", "Susumu Co")
_register_aliases("Rohs", "ROHM")  # guard against a common typo bucket
_register_aliases("Taiyo Yuden", "Taiyo", "Taiyo-Yuden")
_register_aliases("AVX", "AVX Corporation", "Kyocera AVX", "Kyocera")
_register_aliases("Littelfuse", "Littel Fuse")
_register_aliases("Lite-On", "Liteon", "Lite On Technology")
_register_aliases("Everlight", "Everlight Electronics")
_register_aliases("Cree LED", "Cree", "Wolfspeed")
_register_aliases("Osram", "Osram Opto", "Osram Opto Semiconductors", "ams-OSRAM")
_register_aliases("Kingbright", "King Bright")
_register_aliases("Honeywell", "Honeywell Sensing", "Honeywell International")
_register_aliases("Sensata", "Sensata Technologies")
_register_aliases("Bosch Sensortec", "Bosch", "Robert Bosch")
_register_aliases("Nordic Semiconductor", "Nordic", "Nordic Semi")
_register_aliases("Lattice Semiconductor", "Lattice", "SiliconBlue")
_register_aliases("AMD Xilinx", "Xilinx", "AMD")
_register_aliases("Intel", "Altera", "Intel PSG")
_register_aliases("Raspberry Pi", "Raspberry Pi Trading", "RPi")


def normalize_manufacturer(name: object) -> str:
    """Return a canonical manufacturer name.

    Known aliases collapse onto one canonical spelling; unknown names get
    corporate suffixes stripped and title-cased so that ``ACME CORP.`` and
    ``Acme Corporation`` bucket together.
    """
    text = clean(name)
    if not text:
        return ""
    key = slug(text)
    if key in MFR_ALIASES:
        return MFR_ALIASES[key]
    parts = [t for t in tokens(text) if t not in _MFR_NOISE]
    if not parts:
        parts = tokens(text)
    key2 = "".join(parts)
    if key2 in MFR_ALIASES:
        return MFR_ALIASES[key2]
    # Preserve well-known all-caps acronyms, title-case the rest.
    out = []
    for token in parts:
        if len(token) <= 3 and token.isalpha():
            out.append(token.upper())
        elif token.isdigit():
            out.append(token)
        else:
            out.append(token.capitalize())
    return " ".join(out)


def manufacturer_key(name: object) -> str:
    """Bucketing key for a manufacturer (alias-aware)."""
    return slug(normalize_manufacturer(name))


# --------------------------------------------------------------------------- #
# Distributor name normalisation
# --------------------------------------------------------------------------- #

# An aggregator and a direct provider name the same distributor differently
# ("Digi-Key" vs "DigiKey"). Without this, merging their records counts the
# same inventory twice and makes a single-sourced part look multi-sourced.
DISTRIBUTOR_ALIASES: dict[str, str] = {}


def _register_distributor(canonical: str, *aliases: str) -> None:
    for alias in (canonical,) + aliases:
        DISTRIBUTOR_ALIASES[slug(alias)] = canonical


_register_distributor("DigiKey", "Digi-Key", "Digi Key", "DigiKey Electronics",
                      "Digi-Key Electronics", "DK")
_register_distributor("Mouser Electronics", "Mouser", "Mouser Electronics Inc")
_register_distributor("Arrow Electronics", "Arrow", "Arrow Electronics Inc",
                      "Arrow.com", "Verical")
_register_distributor("Farnell", "Farnell element14", "Farnell / element14",
                      "element14", "Premier Farnell", "Newark",
                      "Newark element14", "element14 APAC")
_register_distributor("RS Components", "RS", "RS-Online", "RS Online",
                      "Allied Electronics", "Allied Electronics & Automation")
_register_distributor("LCSC Electronics", "LCSC", "LCSC.com", "Szlcsc")
_register_distributor("Avnet", "Avnet Americas", "Avnet Europe", "Avnet Asia",
                      "Avnet Abacus", "EBV Elektronik")
_register_distributor("TTI", "TTI Inc", "TTI Inc.", "TTI Europe")
_register_distributor("Future Electronics", "Future", "FAI")
_register_distributor("Heilind Electronics", "Heilind")
_register_distributor("Rutronik", "Rutronik24", "Rutronik Elektronische")
_register_distributor("Master Electronics", "Master")
_register_distributor("Sourcengine", "Sourceability")
_register_distributor("Online Components", "OnlineComponents.com")
_register_distributor("Bürklin Elektronik", "Buerklin", "Burklin")
_register_distributor("Chip One Stop", "ChipOneStop", "Chip1Stop")
_register_distributor("Win Source", "WinSource", "Win-Source")
_register_distributor("Component Sense", "ComponentSense")
_register_distributor("Quest Components", "Quest")
_register_distributor("Bisco Industries", "Bisco")


def normalize_distributor(name: object) -> str:
    """Canonical distributor name, so the same seller merges into one offer.

    >>> normalize_distributor("Digi-Key")
    'DigiKey'
    >>> normalize_distributor("element14")
    'Farnell'
    """
    text = clean(name)
    if not text:
        return ""
    key = slug(text)
    if key in DISTRIBUTOR_ALIASES:
        return DISTRIBUTOR_ALIASES[key]
    # Strip corporate suffixes and try again ("Mouser Electronics, Inc.").
    parts = [t for t in tokens(text) if t not in _MFR_NOISE]
    if parts:
        retry = "".join(parts)
        if retry in DISTRIBUTOR_ALIASES:
            return DISTRIBUTOR_ALIASES[retry]
    return text


def distributor_key(name: object) -> str:
    return slug(normalize_distributor(name))


# --------------------------------------------------------------------------- #
# Similarity
# --------------------------------------------------------------------------- #

def ratio(a: str, b: str) -> float:
    """Similarity of two strings in 0..100."""
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    if _HAVE_RAPIDFUZZ:  # pragma: no cover - optional path
        return float(_rf_fuzz.ratio(a, b))
    return SequenceMatcher(None, a, b).ratio() * 100.0


def partial_ratio(a: str, b: str) -> float:
    """Best substring alignment similarity in 0..100."""
    if not a or not b:
        return 0.0
    if _HAVE_RAPIDFUZZ:  # pragma: no cover
        return float(_rf_fuzz.partial_ratio(a, b))
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if short in long:
        return 100.0
    best = 0.0
    window = len(short)
    for i in range(0, len(long) - window + 1):
        best = max(best, SequenceMatcher(None, short, long[i:i + window]).ratio())
        if best == 1.0:
            break
    return best * 100.0


def token_set_ratio(a: str, b: str) -> float:
    """Order/duplication insensitive token similarity in 0..100."""
    ta, tb = set(tokens(a)), set(tokens(b))
    if not ta or not tb:
        return 0.0
    if _HAVE_RAPIDFUZZ:  # pragma: no cover
        return float(_rf_fuzz.token_set_ratio(a, b))
    inter = ta & tb
    only_a = sorted(ta - inter)
    only_b = sorted(tb - inter)
    base = " ".join(sorted(inter))
    s1 = (base + " " + " ".join(only_a)).strip()
    s2 = (base + " " + " ".join(only_b)).strip()
    return max(
        ratio(base, s1) if base else 0.0,
        ratio(base, s2) if base else 0.0,
        ratio(s1, s2),
    )


def best_ratio(a: str, b: str) -> float:
    """Blended similarity used across the app."""
    return max(ratio(a, b), partial_ratio(a, b) * 0.95, token_set_ratio(a, b) * 0.97)


def best_match(needle: str, haystack: Iterable[str], cutoff: float = 0.0
               ) -> tuple[str | None, float]:
    """Return ``(best_candidate, score)`` from ``haystack``."""
    best, best_score = None, -1.0
    for candidate in haystack:
        score = best_ratio(needle, candidate)
        if score > best_score:
            best, best_score = candidate, score
    if best_score < cutoff:
        return None, 0.0
    return best, max(best_score, 0.0)


# --------------------------------------------------------------------------- #
# Reference designators
# --------------------------------------------------------------------------- #

_REFDES_RE = re.compile(r"^([A-Za-z]{1,4})\s*([0-9]{1,6})([A-Za-z]?)$")
_RANGE_SPLIT_RE = re.compile(r"[,;/\n\t]+|\s{2,}")
_RANGE_RE = re.compile(
    r"^([A-Za-z]{1,4})\s*([0-9]{1,6})\s*(?:-|–|—|\.\.|to|through|thru)\s*"
    r"(?:([A-Za-z]{1,4})\s*)?([0-9]{1,6})$",
    re.IGNORECASE,
)

MAX_REFDES_EXPANSION = 5000


def split_refdes(value: object) -> list[str]:
    """Split a reference-designator cell into individual designators.

    Understands comma, semicolon, slash, newline and whitespace separation, and
    expands ranges such as ``R1-R8``, ``C10..C14``, ``U1 to U3``.

    >>> split_refdes("R1, R2,R3-R5 ; C9")
    ['R1', 'R2', 'R3', 'R4', 'R5', 'C9']
    """
    text = clean(value)
    if not text:
        return []
    out: list[str] = []
    chunks = [c.strip() for c in _RANGE_SPLIT_RE.split(text) if c.strip()]
    if len(chunks) == 1 and " " in chunks[0]:
        # Space separated list: "R1 R2 R3"
        pieces = chunks[0].split(" ")
        if len(pieces) > 1 and all(_REFDES_RE.match(p) for p in pieces):
            chunks = pieces
    for chunk in chunks:
        match = _RANGE_RE.match(chunk)
        if match:
            prefix, start, prefix2, end = match.groups()
            if prefix2 and prefix2.upper() != prefix.upper():
                out.append(f"{prefix.upper()}{start}")
                out.append(f"{prefix2.upper()}{end}")
                continue
            lo, hi = int(start), int(end)
            if lo > hi:
                lo, hi = hi, lo
            if hi - lo > MAX_REFDES_EXPANSION:
                out.append(chunk.upper())
                continue
            out.extend(f"{prefix.upper()}{n}" for n in range(lo, hi + 1))
            continue
        simple = _REFDES_RE.match(chunk)
        if simple:
            prefix, number, tail = simple.groups()
            out.append(f"{prefix.upper()}{number}{tail.upper()}")
        else:
            out.append(chunk.upper())
    # De-duplicate, preserve order.
    seen: set[str] = set()
    unique: list[str] = []
    for ref in out:
        if ref not in seen:
            seen.add(ref)
            unique.append(ref)
    return unique


def refdes_prefix(ref: str) -> str:
    match = _REFDES_RE.match(clean(ref))
    return match.group(1).upper() if match else ""


def collapse_refdes(refs: Sequence[str]) -> str:
    """Inverse of :func:`split_refdes` -- compact a list back into ranges.

    >>> collapse_refdes(["R1", "R2", "R3", "R7", "C1"])
    'R1-R3, R7, C1'
    """
    if not refs:
        return ""
    groups: list[tuple[str, list[int], list[str]]] = []
    for ref in refs:
        match = _REFDES_RE.match(clean(ref))
        if match and not match.group(3):
            prefix, number = match.group(1).upper(), int(match.group(2))
            if groups and groups[-1][0] == prefix:
                groups[-1][1].append(number)
            else:
                groups.append((prefix, [number], []))
        else:
            groups.append(("", [], [clean(ref).upper()]))
    parts: list[str] = []
    for prefix, numbers, literals in groups:
        if literals:
            parts.extend(literals)
            continue
        numbers = sorted(set(numbers))
        run_start = numbers[0]
        previous = numbers[0]
        for number in numbers[1:] + [None]:  # type: ignore[list-item]
            if number is not None and number == previous + 1:
                previous = number
                continue
            if run_start == previous:
                parts.append(f"{prefix}{run_start}")
            elif previous == run_start + 1:
                parts.append(f"{prefix}{run_start}")
                parts.append(f"{prefix}{previous}")
            else:
                parts.append(f"{prefix}{run_start}-{prefix}{previous}")
            if number is not None:
                run_start = previous = number
    return ", ".join(parts)


def split_list_cell(value: object) -> list[str]:
    """Split a multi-value cell (alt MPNs, approvals) into trimmed items."""
    text = clean(value)
    if not text:
        return []
    items = [p.strip() for p in re.split(r"[,;|\n]+|\s/\s", text)]
    return [i for i in items if i]


def truncate(text: str, limit: int = 120) -> str:
    text = clean(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
