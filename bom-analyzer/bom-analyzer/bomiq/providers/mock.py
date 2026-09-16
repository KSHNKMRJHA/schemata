"""
Offline catalogue provider.

This provider never touches the network. It exists so that:

* the application is fully usable and demonstrable before any API key exists;
* the whole pipeline (matching, risk, cost, alternates, compliance, export) can
  be unit-tested deterministically;
* an air-gapped machine still gets validation, de-duplication and reporting.

**The data it returns is synthetic.** It is derived deterministically from the
part number, seeded with a curated table of real, widely-used part numbers so
that demos look realistic. Every record is stamped
``Data source: Offline demo catalogue (synthetic)`` and every offer carries a
warning, and the engine marks the whole analysis ``offline`` so no one mistakes
these figures for live distributor data.
"""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from typing import Any

from ..core.models import (
    Compliance, ComplianceState, Lifecycle, Offer, PartData, PriceBreak,
)
from ..util.text import clean, normalize_manufacturer, normalize_mpn, tokens
from ..util.units import normalize_package
from .base import Provider

SYNTHETIC_NOTE = "Offline demo catalogue (synthetic)"

_DISTRIBUTORS = [
    ("DigiKey", "US", 1.00),
    ("Mouser Electronics", "US", 1.02),
    ("Arrow Electronics", "US", 0.98),
    ("Farnell / element14", "EMEA", 1.08),
    ("LCSC Electronics", "APAC", 0.72),
    ("TTI Inc.", "US", 1.05),
]

# --------------------------------------------------------------------------- #
# Curated seed catalogue -- real part numbers, plausible attributes.
# fields: manufacturer, description, package, category, lifecycle, base price
# --------------------------------------------------------------------------- #

SEED: dict[str, dict[str, Any]] = {
    # -- resistors ------------------------------------------------------- #
    "RC0603FR-0710KL": dict(mfr="Yageo", desc="RES SMD 10K OHM 1% 1/10W 0603",
                            pkg="0603", cat="Resistors", price="0.0032"),
    "RC0603FR-07100RL": dict(mfr="Yageo", desc="RES SMD 100 OHM 1% 1/10W 0603",
                             pkg="0603", cat="Resistors", price="0.0032"),
    "RC0402FR-071KL": dict(mfr="Yageo", desc="RES SMD 1K OHM 1% 1/16W 0402",
                           pkg="0402", cat="Resistors", price="0.0021"),
    "CRCW060310K0FKEA": dict(mfr="Vishay", desc="RES SMD 10K OHM 1% 1/10W 0603",
                             pkg="0603", cat="Resistors", price="0.0104"),
    "ERJ-3EKF1002V": dict(mfr="Panasonic", desc="RES SMD 10K OHM 1% 1/10W 0603",
                          pkg="0603", cat="Resistors", price="0.0098"),
    "RK73H1JTTD1002F": dict(mfr="KOA Speer", desc="RES SMD 10K OHM 1% 1/16W 0603",
                            pkg="0603", cat="Resistors", price="0.0087"),
    # -- capacitors ------------------------------------------------------ #
    "GRM188R71C104KA01D": dict(mfr="Murata Electronics",
                               desc="CAP CER 0.1UF 16V X7R 0603", pkg="0603",
                               cat="Capacitors", price="0.0182"),
    "GRM21BR61E106KA73L": dict(mfr="Murata Electronics",
                               desc="CAP CER 10UF 25V X5R 0805", pkg="0805",
                               cat="Capacitors", price="0.0940"),
    "C0603C104K5RACTU": dict(mfr="KEMET", desc="CAP CER 0.1UF 50V X7R 0603",
                             pkg="0603", cat="Capacitors", price="0.0246"),
    "CL10B104KB8NNNC": dict(mfr="Samsung Electro-Mechanics",
                            desc="CAP CER 0.1UF 50V X7R 0603", pkg="0603",
                            cat="Capacitors", price="0.0118"),
    "TAJB106K016RNJ": dict(mfr="AVX", desc="CAP TANT 10UF 16V 10% 1210",
                           pkg="1210", cat="Capacitors", price="0.4300",
                           lifecycle="NRND"),
    "EEE-FK1V101P": dict(mfr="Panasonic",
                         desc="CAP ALUM 100UF 35V 20% SMD", pkg="SMD",
                         cat="Capacitors", price="0.5100"),
    # -- inductors ------------------------------------------------------- #
    "744773022": dict(mfr="Wurth Elektronik", desc="IND PWR 2.2UH 4.6A SMD",
                      pkg="1210", cat="Inductors", price="0.5800"),
    "XAL4020-222MEC": dict(mfr="Coilcraft", desc="IND PWR 2.2UH 8.7A SMD",
                           pkg="4020", cat="Inductors", price="0.9200"),
    "BLM18PG221SN1D": dict(mfr="Murata Electronics",
                           desc="FERRITE BEAD 220 OHM 0603 1LN", pkg="0603",
                           cat="Inductors", price="0.0530"),
    # -- semiconductors -------------------------------------------------- #
    "LM358DR": dict(mfr="Texas Instruments",
                    desc="IC OPAMP GP 2 CIRCUIT SOIC-8", pkg="SOIC-8",
                    cat="Integrated Circuits", price="0.2900"),
    "NE555DR": dict(mfr="Texas Instruments", desc="IC TIMER SGL 100KHZ SOIC-8",
                    pkg="SOIC-8", cat="Integrated Circuits", price="0.4200"),
    "LM7805CT": dict(mfr="ON Semiconductor",
                     desc="IC REG LINEAR 5V 1A TO-220-3", pkg="TO-220-3",
                     cat="Integrated Circuits", price="0.5800",
                     lifecycle="NRND"),
    "AMS1117-3.3": dict(mfr="Advanced Monolithic Systems",
                        desc="IC REG LDO 3.3V 1A SOT-223", pkg="SOT-223",
                        cat="Integrated Circuits", price="0.1400"),
    "TPS62840DLCR": dict(mfr="Texas Instruments",
                         desc="IC REG BUCK 1.8-6.5V 750MA SOT-563",
                         pkg="SOT-563", cat="Integrated Circuits",
                         price="0.9800"),
    "STM32F103C8T6": dict(mfr="STMicroelectronics",
                          desc="IC MCU 32BIT 64KB FLASH LQFP-48",
                          pkg="LQFP-48", cat="Integrated Circuits",
                          price="2.4500"),
    "STM32F407VGT6": dict(mfr="STMicroelectronics",
                          desc="IC MCU 32BIT 1MB FLASH LQFP-100",
                          pkg="LQFP-100", cat="Integrated Circuits",
                          price="9.8000"),
    "ATMEGA328P-AU": dict(mfr="Microchip Technology",
                          desc="IC MCU 8BIT 32KB FLASH TQFP-32",
                          pkg="TQFP-32", cat="Integrated Circuits",
                          price="2.1000"),
    "ESP32-WROOM-32E": dict(mfr="Espressif Systems",
                            desc="MODULE WIFI/BT MCU 4MB FLASH",
                            pkg="SMD Module", cat="RF Modules",
                            price="2.8000"),
    "MCP2551-I/SN": dict(mfr="Microchip Technology",
                         desc="IC TRANSCEIVER CAN 1/1 SOIC-8", pkg="SOIC-8",
                         cat="Interface", price="1.1200", lifecycle="NRND"),
    "MAX3232ECPE+": dict(mfr="Analog Devices",
                         desc="IC TRANSCEIVER RS232 2/2 DIP-16",
                         pkg="DIP-16", cat="Interface", price="3.9000",
                         lifecycle="EOL"),
    "FT232RL": dict(mfr="FTDI", desc="IC USB UART BRIDGE SSOP-28",
                    pkg="SSOP-28", cat="Interface", price="4.5000"),
    "SN74HC595N": dict(mfr="Texas Instruments",
                       desc="IC 8-BIT SHIFT REGISTER DIP-16", pkg="DIP-16",
                       cat="Logic", price="0.5200"),
    "24LC256-I/SN": dict(mfr="Microchip Technology",
                         desc="IC EEPROM 256KBIT I2C SOIC-8", pkg="SOIC-8",
                         cat="Memory", price="0.6400"),
    "W25Q128JVSIQ": dict(mfr="Winbond Electronics",
                         desc="IC FLASH 128MBIT SPI SOIC-8", pkg="SOIC-8",
                         cat="Memory", price="1.3800"),
    # -- discretes ------------------------------------------------------- #
    "1N4148W-7-F": dict(mfr="Diodes Incorporated",
                        desc="DIODE SWITCH 100V 300MA SOD-123", pkg="SOD-123",
                        cat="Diodes", price="0.0290"),
    "SS34": dict(mfr="ON Semiconductor",
                 desc="DIODE SCHOTTKY 40V 3A DO-214AB", pkg="DO-214AB",
                 cat="Diodes", price="0.1500"),
    "BAT54S": dict(mfr="Nexperia", desc="DIODE SCHOTTKY 30V 200MA SOT-23",
                   pkg="SOT-23", cat="Diodes", price="0.0670"),
    "BC847B": dict(mfr="Nexperia", desc="TRANS NPN 45V 100MA SOT-23",
                   pkg="SOT-23", cat="Transistors", price="0.0310"),
    "IRLML6244TRPBF": dict(mfr="Infineon Technologies",
                           desc="MOSFET N-CH 20V 6.3A SOT-23", pkg="SOT-23",
                           cat="Transistors", price="0.4400"),
    "AO3400A": dict(mfr="Alpha & Omega", desc="MOSFET N-CH 30V 5.7A SOT-23",
                    pkg="SOT-23", cat="Transistors", price="0.0980"),
    "SMAJ24CA": dict(mfr="Littelfuse", desc="TVS DIODE 24V 400W DO-214AC",
                     pkg="DO-214AC", cat="Circuit Protection", price="0.2100"),
    # -- optoelectronics ------------------------------------------------- #
    "150060GS75000": dict(mfr="Wurth Elektronik", desc="LED GREEN CLEAR 0603",
                          pkg="0603", cat="LEDs", price="0.1200"),
    "LTST-C170KRKT": dict(mfr="Lite-On", desc="LED RED CLEAR 0805",
                          pkg="0805", cat="LEDs", price="0.1900"),
    # -- crystals / timing ----------------------------------------------- #
    "ABM8-8.000MHZ-B2-T": dict(mfr="Abracon", desc="CRYSTAL 8.0000MHZ 18PF SMD",
                               pkg="3225", cat="Crystals", price="0.4800"),
    "ABLS-16.000MHZ-B4-T": dict(mfr="Abracon",
                                desc="CRYSTAL 16.0000MHZ 18PF SMD",
                                pkg="5032", cat="Crystals", price="0.3900"),
    "ECS-.327-12.5-34B-TR": dict(mfr="ECS", desc="CRYSTAL 32.768KHZ 12.5PF SMD",
                                 pkg="3215", cat="Crystals", price="0.3300"),
    # -- connectors ------------------------------------------------------ #
    "61300411121": dict(mfr="Wurth Elektronik",
                        desc="CONN HEADER VERT 4POS 2.54MM", pkg="THT",
                        cat="Connectors", price="0.2500"),
    "B3B-EH-A(LF)(SN)": dict(mfr="JST", desc="CONN HEADER R/A 3POS 2.5MM",
                             pkg="THT", cat="Connectors", price="0.3400"),
    "10118193-0001LF": dict(mfr="Amphenol", desc="CONN RCPT USB2.0 MICRO B SMD",
                            pkg="SMD", cat="Connectors", price="0.5600"),
    "USB4085-GF-A": dict(mfr="GCT", desc="CONN RCPT USB TYPE C 24POS SMD",
                         pkg="SMD", cat="Connectors", price="0.8900"),
    "1725656": dict(mfr="Phoenix Contact", desc="TERM BLOCK 2POS 5.08MM PCB",
                    pkg="THT", cat="Connectors", price="1.1000"),
    # -- electromechanical ----------------------------------------------- #
    "TL3342F160QG": dict(mfr="E-Switch", desc="SWITCH TACTILE SPST-NO 50MA 12V",
                         pkg="SMD", cat="Switches", price="0.1300"),
    "0451002.MRL": dict(mfr="Littelfuse", desc="FUSE BOARD MOUNT 2A 125V SMD",
                        pkg="1206", cat="Circuit Protection", price="0.3200"),
    "G5LE-14-DC12": dict(mfr="Omron", desc="RELAY GEN PURPOSE SPDT 10A 12V",
                         pkg="THT", cat="Relays", price="1.6800"),
    # -- sensors --------------------------------------------------------- #
    "BME280": dict(mfr="Bosch Sensortec",
                   desc="SENSOR HUMIDITY/PRESS/TEMP I2C/SPI LGA-8",
                   pkg="LGA-8", cat="Sensors", price="4.6000"),
    "MPU-6050": dict(mfr="TDK Corporation",
                     desc="SENSOR 6-AXIS IMU I2C QFN-24", pkg="QFN-24",
                     cat="Sensors", price="3.2000", lifecycle="Obsolete"),
    "TMP102AIDRLR": dict(mfr="Texas Instruments",
                         desc="SENSOR TEMP DIGITAL I2C SOT-563",
                         pkg="SOT-563", cat="Sensors", price="1.0500"),
}

# Alternate groups: drop-in or near drop-in families.
ALTERNATE_GROUPS: list[list[str]] = [
    ["RC0603FR-0710KL", "CRCW060310K0FKEA", "ERJ-3EKF1002V",
     "RK73H1JTTD1002F"],
    ["GRM188R71C104KA01D", "C0603C104K5RACTU", "CL10B104KB8NNNC"],
    ["LM358DR", "MC1458DR2G", "TL072CDR"],
    ["1N4148W-7-F", "1N4148WS-7-F", "BAS316"],
    ["BC847B", "MMBT3904", "BC817-25"],
    ["AO3400A", "SI2302CDS-T1-GE3", "IRLML6244TRPBF"],
    ["SS34", "SK34A", "B340A-13-F"],
    ["AMS1117-3.3", "LM1117MPX-3.3", "TLV1117-33IDCYR"],
    ["ABM8-8.000MHZ-B2-T", "ECS-80-18-5PX-TR", "7M-8.000MAAJ-T"],
    ["MAX3232ECPE+", "SP3232EEN-L", "ADM3202ARNZ"],
    ["MCP2551-I/SN", "TJA1050T/CM,118", "SN65HVD230DR"],
]

_ALT_INDEX: dict[str, list[str]] = {}
for _group in ALTERNATE_GROUPS:
    for _mpn in _group:
        _ALT_INDEX[normalize_mpn(_mpn)] = [m for m in _group if m != _mpn]


class MockProvider(Provider):
    """Deterministic offline catalogue. Never makes a network request."""

    id = "mock"
    can_suggest_alternates = True

    def __init__(self, config: Any, http: Any | None = None,
                 part_cache: Any | None = None) -> None:
        # Deliberately no HTTP client: nothing here can reach the network.
        super().__init__(config, http=http, part_cache=None)
        self._catalogue_keys = {normalize_mpn(key): key for key in SEED}

    def check_credentials(self) -> tuple[bool, str]:
        return True, "Offline catalogue — no credentials required"

    def self_test(self) -> dict[str, Any]:
        part = self.fetch("RC0603FR-0710KL")
        return {
            "provider": self.id, "ok": part is not None,
            "message": "Offline catalogue is available "
                       "(synthetic data, no network)",
            "latency_ms": 0,
        }

    # -- deterministic pseudo-randomness ---------------------------------- #

    @staticmethod
    def _seed(mpn: str, salt: str = "") -> int:
        digest = hashlib.sha256(
            f"{normalize_mpn(mpn)}|{salt}".encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big")

    def _rand(self, mpn: str, salt: str, lo: float, hi: float) -> float:
        span = max(0.0, hi - lo)
        return lo + span * ((self._seed(mpn, salt) % 100_000) / 100_000.0)

    def _choice(self, mpn: str, salt: str, options: list[Any]) -> Any:
        return options[self._seed(mpn, salt) % len(options)]

    # -- lookup ----------------------------------------------------------- #

    def fetch(self, mpn: str, manufacturer: str = "") -> PartData | None:
        term = clean(mpn)
        if not term:
            return None
        key = normalize_mpn(term)
        if len(key) < 3:
            return None

        seed = SEED.get(self._catalogue_keys.get(key, ""), None)
        if seed is None:
            # Unknown part: still return a plausible record so the pipeline can
            # be exercised, unless the term looks like junk.
            if not re.search(r"\d", key) or len(key) < 4:
                return None
            seed = self._synthesise_seed(term, manufacturer)
            canonical_mpn = term
        else:
            canonical_mpn = self._catalogue_keys[key]

        lifecycle, lifecycle_note = self._lifecycle(canonical_mpn, seed)
        part = PartData(
            mpn=canonical_mpn,
            manufacturer=normalize_manufacturer(seed["mfr"]),
            description=seed["desc"],
            category=seed.get("cat", ""),
            package=normalize_package(seed.get("pkg", "")),
            lifecycle=lifecycle,
            lifecycle_note=lifecycle_note,
            datasheet_url=f"https://example.invalid/datasheets/"
                          f"{key.lower()}.pdf",
            product_url="",
            providers=[self.id],
        )
        part.specs["Data source"] = SYNTHETIC_NOTE
        if seed.get("synthetic"):
            # Not in the curated table: keep the record (so the pipeline can be
            # exercised) but make its uncertainty explicit everywhere.
            part.specs["Record type"] = (
                "Synthesised — this part is not in the offline catalogue, "
                "so attributes are placeholders")
            part.lifecycle = Lifecycle.UNKNOWN
            part.lifecycle_note = "Not in the offline catalogue"
        part.specs["Category"] = seed.get("cat", "")
        if seed.get("pkg"):
            part.specs["Package / Case"] = seed["pkg"]
        part.specs["RoHS Status"] = "RoHS3 Compliant"
        part.specs["Moisture Sensitivity Level (MSL)"] = str(
            self._choice(canonical_mpn, "msl", ["1", "1", "2", "3"]))
        if seed.get("cat") in ("Resistors", "Capacitors", "Inductors"):
            part.specs["Tolerance"] = self._choice(
                canonical_mpn, "tol", ["±1%", "±5%", "±10%"])

        part.compliance = self._compliance(canonical_mpn, lifecycle)
        part.offers = self._offers(canonical_mpn, seed, lifecycle)
        part.alternate_mpns = list(_ALT_INDEX.get(key, []))
        part.similar_mpns = self._similar(canonical_mpn, seed)
        part.median_price_1k = self._base_price(seed) * Decimal("0.62")
        part.median_price_1k_currency = "USD"
        part.estimated_factory_lead_days = int(
            self._rand(canonical_mpn, "factory", 42, 182))
        part.total_avail = sum(offer.stock or 0 for offer in part.offers)
        return part

    def _synthesise_seed(self, mpn: str, manufacturer: str) -> dict[str, Any]:
        """Invent a plausible record for a part not in the seed table."""
        category, package, price = self._guess_from_mpn(mpn)
        return {
            "mfr": normalize_manufacturer(manufacturer) or self._choice(
                mpn, "mfr", ["Generic Components", "Unlisted Manufacturer"]),
            "desc": f"{category[:-1] if category.endswith('s') else category} "
                    f"(offline record, details unverified)",
            "pkg": package,
            "cat": category,
            "price": price,
            "synthetic": True,
        }

    def _guess_from_mpn(self, mpn: str) -> tuple[str, str, str]:
        text = mpn.upper()
        if re.match(r"^(RC|CRCW|ERJ|RK73|RMCF|MCR|AC\d)", text):
            return "Resistors", self._chip_size(text) or "0603", "0.0040"
        if re.match(r"^(GRM|C0|CL\d|CC\d|GCM|UMK|TAJ|T49|EEE|EEU)", text):
            return "Capacitors", self._chip_size(text) or "0603", "0.0200"
        if re.match(r"^(BLM|MPZ|DLW|744|XAL|XFL|SRR|LQ)", text):
            return "Inductors", self._chip_size(text) or "0805", "0.1500"
        if re.match(r"^(1N|BAT|BAS|SS\d|SK\d|SMA|SMB|MBR)", text):
            return "Diodes", "SOD-123", "0.0500"
        if re.match(r"^(BC|MMBT|2N|AO\d|SI2|IRL|BSS|DMN|FDN)", text):
            return "Transistors", "SOT-23", "0.0800"
        if re.match(r"^(STM32|ATMEGA|ATSAM|PIC\d|MSP430|NRF5|ESP)", text):
            return "Integrated Circuits", "LQFP-48", "3.5000"
        if re.match(r"^(LM|TL|OPA|AD\d|MAX|MCP|NE\d|SN74|74HC|CD4)", text):
            return "Integrated Circuits", "SOIC-8", "0.6000"
        return "Uncategorised", "", "0.2500"

    @staticmethod
    def _chip_size(text: str) -> str:
        match = re.search(r"\b(0201|0402|0603|0805|1206|1210|1812|2010|2512)\b",
                          text)
        if match:
            return match.group(1)
        match = re.match(r"^[A-Z]{2,4}(0402|0603|0805|1206|1210)", text)
        return match.group(1) if match else ""

    def _lifecycle(self, mpn: str, seed: dict[str, Any]
                   ) -> tuple[Lifecycle, str]:
        explicit = seed.get("lifecycle")
        if explicit:
            state = Lifecycle(explicit)
            return state, f"{state.value} (offline catalogue)"
        roll = self._seed(mpn, "lifecycle") % 100
        if roll < 76:
            return Lifecycle.ACTIVE, "Active (offline catalogue)"
        if roll < 84:
            return Lifecycle.NEW, "New product (offline catalogue)"
        if roll < 92:
            return Lifecycle.NRND, "Not recommended for new designs " \
                                   "(offline catalogue)"
        if roll < 97:
            return Lifecycle.EOL, "End of life (offline catalogue)"
        return Lifecycle.OBSOLETE, "Obsolete (offline catalogue)"

    def _compliance(self, mpn: str, lifecycle: Lifecycle) -> Compliance:
        roll = self._seed(mpn, "rohs") % 100
        rohs = ComplianceState.COMPLIANT
        note = "RoHS3 Compliant"
        if roll >= 94:
            rohs = ComplianceState.NON_COMPLIANT
            note = "Not RoHS compliant"
        elif roll >= 90:
            rohs = ComplianceState.EXEMPT
            note = "RoHS compliant by exemption 7(c)-I"
        reach_roll = self._seed(mpn, "reach") % 100
        svhc: list[str] = []
        reach = ComplianceState.COMPLIANT
        if reach_roll >= 95:
            reach = ComplianceState.NON_COMPLIANT
            svhc = ["Lead monoxide (CAS 1317-36-8)"]
        elif reach_roll >= 88:
            reach = ComplianceState.UNKNOWN
        return Compliance(
            rohs=rohs, rohs_note=note, reach=reach, reach_note="",
            svhc=svhc,
            halogen_free=ComplianceState.COMPLIANT
            if self._seed(mpn, "halogen") % 10 > 2 else ComplianceState.UNKNOWN,
            country_of_origin=self._choice(
                mpn, "coo", ["CN", "TW", "JP", "MY", "PH", "TH", "MX", "US",
                             "DE", "KR", "VN"]),
            hts_code=self._choice(
                mpn, "hts", ["8533.21.0000", "8532.24.0020", "8541.10.0080",
                             "8542.31.0001", "8536.69.4051", "8504.31.4000"]),
            eccn=self._choice(mpn, "eccn", ["EAR99", "EAR99", "EAR99",
                                            "3A991", "5A992.c"]),
            export_controlled=self._seed(mpn, "export") % 50 == 0,
            itar=False,
            msl=str(self._choice(mpn, "msl", ["1", "1", "2", "3"])),
            aec_q="AEC-Q200" if self._seed(mpn, "aec") % 4 == 0 else "",
            sources=[SYNTHETIC_NOTE],
        )

    def _base_price(self, seed: dict[str, Any]) -> Decimal:
        try:
            return Decimal(str(seed.get("price", "0.25")))
        except Exception:  # pragma: no cover
            return Decimal("0.25")

    def _offers(self, mpn: str, seed: dict[str, Any],
                lifecycle: Lifecycle) -> list[Offer]:
        base = self._base_price(seed)
        count = 2 + self._seed(mpn, "dcount") % 4
        if lifecycle in (Lifecycle.OBSOLETE,):
            count = max(0, count - 3)
        elif lifecycle in (Lifecycle.EOL, Lifecycle.NRND):
            count = max(1, count - 2)

        offers: list[Offer] = []
        for index in range(count):
            distributor, region, factor = _DISTRIBUTORS[
                (self._seed(mpn, f"dist{index}") + index) % len(_DISTRIBUTORS)]
            if any(offer.distributor == distributor for offer in offers):
                continue
            stock_roll = self._seed(mpn, f"stock{index}") % 100
            if lifecycle is Lifecycle.OBSOLETE:
                stock = 0
            elif lifecycle in (Lifecycle.EOL, Lifecycle.NRND):
                stock = 0 if stock_roll < 45 else int(
                    self._rand(mpn, f"s{index}", 50, 4_000))
            elif stock_roll < 8:
                stock = 0
            else:
                stock = int(self._rand(mpn, f"s{index}", 250, 250_000))

            unit = base * Decimal(str(round(factor, 4)))
            spq = int(self._choice(mpn, f"spq{index}", [1, 1, 10, 100, 1000,
                                                        2500, 4000, 5000]))
            moq = spq if spq > 1 else int(
                self._choice(mpn, f"moq{index}", [1, 1, 1, 10, 100]))
            ladder = [
                (1, unit * Decimal("1.00")),
                (10, unit * Decimal("0.86")),
                (100, unit * Decimal("0.68")),
                (1000, unit * Decimal("0.52")),
                (10000, unit * Decimal("0.43")),
            ]
            breaks = [
                PriceBreak(quantity=quantity,
                           unit_price=price.quantize(Decimal("0.00001")),
                           currency="USD")
                for quantity, price in ladder if quantity >= moq or quantity == 1
            ]
            lead = 0 if stock else int(self._rand(mpn, f"lead{index}", 28, 210))
            offer = self.make_offer(
                distributor=distributor,
                sku=f"{distributor[:3].upper()}-{normalize_mpn(mpn)[:14]}"
                    f"-{index + 1}",
                packaging=self._choice(mpn, f"pack{index}",
                                       ["Cut Tape", "Tape & Reel", "Bulk",
                                        "Tube", "Digi-Reel"]),
                stock=stock, moq=moq, spq=spq, order_multiple=spq,
                lead_time_days=lead, price_breaks=breaks, currency="USD",
                region=region, authorized=True,
                url="",
                warnings=[f"{SYNTHETIC_NOTE} — not live pricing"],
            )
            offers.append(offer)
        return offers

    def _similar(self, mpn: str, seed: dict[str, Any]) -> list[str]:
        """Same-category catalogue neighbours, used for alternate hunting."""
        category = seed.get("cat", "")
        if not category:
            return []
        neighbours = [
            key for key, value in SEED.items()
            if value.get("cat") == category and normalize_mpn(key) !=
            normalize_mpn(mpn)
        ]
        neighbours.sort(key=lambda key: self._seed(mpn, key) % 1000)
        return neighbours[:6]

    # -- search / alternates ---------------------------------------------- #

    def search(self, query: str, limit: int = 10) -> list[PartData]:
        """Keyword search across the seed catalogue's descriptions."""
        wanted = set(tokens(query))
        if not wanted:
            return []
        scored: list[tuple[int, str]] = []
        for key, value in SEED.items():
            haystack = set(tokens(f"{key} {value['mfr']} {value['desc']} "
                                  f"{value.get('pkg', '')}"))
            overlap = len(wanted & haystack)
            if overlap:
                scored.append((overlap, key))
        scored.sort(key=lambda item: (-item[0], item[1]))
        out: list[PartData] = []
        for _, key in scored[:limit]:
            part = self.fetch(key)
            if part:
                out.append(part)
        return out

    def alternates(self, part: PartData, limit: int = 10) -> list[PartData]:
        candidates = list(part.alternate_mpns) + list(part.similar_mpns)
        out: list[PartData] = []
        for candidate in candidates[:limit * 2]:
            record = self.fetch(candidate)
            if record is not None:
                out.append(record)
            if len(out) >= limit:
                break
        return out
