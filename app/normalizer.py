"""Part Normalizer: clean MPN, guess manufacturer, flag package/suffix hints."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Heuristic manufacturer-by-prefix map. Order matters (longest prefix first).
_MANUFACTURER_PREFIXES: list[tuple[str, str]] = [
    ("STM32", "STMicroelectronics"),
    ("STM8", "STMicroelectronics"),
    ("ST", "STMicroelectronics"),
    ("ATTINY", "Microchip"),
    ("ATMEGA", "Microchip"),
    ("AT90", "Microchip"),
    ("AT28", "Microchip"),
    ("AT25", "Microchip"),
    ("AT24", "Microchip"),
    ("ATSAM", "Microchip"),
    ("ATSAMD", "Microchip"),
    ("PIC16", "Microchip"),
    ("PIC18", "Microchip"),
    ("PIC24", "Microchip"),
    ("ESP32", "Espressif"),
    ("ESP8266", "Espressif"),
    ("NRF52", "Nordic Semiconductor"),
    ("NRF51", "Nordic Semiconductor"),
    ("MSP430", "Texas Instruments"),
    ("CC13", "Texas Instruments"),
    ("CC26", "Texas Instruments"),
    ("TPS", "Texas Instruments"),
    ("LM", "Texas Instruments"),
    ("OPA", "Texas Instruments"),
    ("TLC", "Texas Instruments"),
    ("ADS", "Texas Instruments"),
    ("DAC", "Texas Instruments"),
    ("ADC", "Texas Instruments"),
    ("SN74", "Texas Instruments"),
    ("CD74", "Texas Instruments"),
    ("LPC", "NXP"),
    ("LM", "NXP"),
    ("74HC", "Nexperia"),
    ("74LVC", "Nexperia"),
    ("BC", "Nexperia"),
    ("NE555", "Texas Instruments"),
    ("SG3525", "STMicroelectronics"),
    ("USB", "Microchip"),
]

_SANITIZE = re.compile(r"[\u00a0]+")


@dataclass
class NormalizedPart:
    mpn_raw: str
    mpn_normalized: str
    manufacturer_hint: str = ""
    package_hint: str = ""
    aliases: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def normalize(mpn: str, manufacturer: str | None = None) -> NormalizedPart:
    """Return a cleaned, unambiguous MPN plus manufacturer/package hints."""
    raw = (mpn or "").strip()
    cleaned = _SANITIZE.sub(" ", raw).strip().upper()
    cleaned = cleaned.strip(" -.\t")
    cleaned = re.sub(r"\s+", " ", cleaned)

    mfr = (manufacturer or "").strip()
    if not mfr:
        mfr = guess_manufacturer(cleaned)
    else:
        mfr = normalize_manufacturer_name(mfr)

    package = _guess_package(cleaned, raw)

    notes: list[str] = []
    if cleaned != raw:
        notes.append(f"Normalized input '{raw}' -> '{cleaned}'")
    if package:
        notes.append(f"Package/suffix hint detected: {package}")

    return NormalizedPart(
        mpn_raw=raw,
        mpn_normalized=cleaned,
        manufacturer_hint=mfr,
        package_hint=package,
        aliases=[raw, cleaned],
        notes=notes,
    )


def guess_manufacturer(mpn_normalized: str) -> str:
    for prefix, mfr in _MANUFACTURER_PREFIXES:
        if mpn_normalized.startswith(prefix):
            return mfr
    return ""


def normalize_manufacturer_name(name: str) -> str:
    mapping = {
        "TI": "Texas Instruments",
        "TEXAS": "Texas Instruments",
        "TEXAS INSTRUMENTS": "Texas Instruments",
        "STM": "STMicroelectronics",
        "STMICRO": "STMicroelectronics",
        "ST MICROELECTRONICS": "STMicroelectronics",
        "ATMEL": "Microchip",
        "MICROCHIP TECHNOLOGY": "Microchip",
        "NXP SEMICONDUCTORS": "NXP",
        "NXPSEMICONDUCTORSN.V.": "NXP",
        "INFINEON TECHNOLOGIES": "Infineon",
        "FAIRCHILD": "ON Semiconductor",
        "ON SEMI": "ON Semiconductor",
    }
    key = name.strip().upper()
    return mapping.get(key, name.strip())


_PACKAGE_SUFFIXES = {
    "T6": "LQFP144",
    "VGT6": "LQFP100",
    "VET6": "LQFP100",
    "VIT6": "LQFP100",
    "RGT6": "LQFP64",
    "RET6": "LQFP64",
    "RCT6": "LQFP64",
    "C8T6": "LQFP48",
    "CBT6": "LQFP48",
    "C6T6": "LQFP48",
    "CBU7": "UFBGA48",
    "JTRS6": "WLCSP",
    "JYT6": "WLCSP",
}


def _guess_package(mpn_normalized: str, raw: str) -> str:
    for suffix, pkg in sorted(_PACKAGE_SUFFIXES.items(), key=lambda kv: -len(kv[0])):
        if mpn_normalized.endswith(suffix) and mpn_normalized != suffix:
            return pkg
    # Generic "package-like" tokens: "-LF", "SOIC-8", "-DIP", etc.
    pkg_re = (
        r"(SOIC[- ]?\d+|LQFP[- ]?\d+|TSSOP[- ]?\d+|QFN[- ]?\d+|DIP[- ]?\d+"
        r"|SOT23[- ]?\d*|SOP[- ]?\d+)"
    )
    m = re.search(pkg_re, raw, re.I)
    if m:
        return m.group(1).upper()
    return ""
