#!/usr/bin/env python3
"""
Generate the sample BOMs used by the tests and the demo.

Deliberately awkward on purpose: every file here exercises a different real
world annoyance -- a title block above the table, stacked headers, European
decimal commas, designator ranges, duplicate lines, a DNP column with inverted
sense, a multi-sheet workbook, an ERP export with only internal part numbers,
and a file where Excel has mangled a part number into scientific notation.

    python samples/make_samples.py
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def write_csv(name: str, rows: list[list[object]], delimiter: str = ",",
              encoding: str = "utf-8") -> Path:
    path = HERE / name
    with path.open("w", encoding=encoding, newline="") as handle:
        writer = csv.writer(handle, delimiter=delimiter)
        writer.writerows(rows)
    return path


# --------------------------------------------------------------------------- #
# 1. Altium-style export: title block, then the table
# --------------------------------------------------------------------------- #

ALTIUM = [
    ["Bill of Materials for Project [Sensor Hub Rev C]"],
    ["Project:", "Sensor Hub"],
    ["Revision:", "C"],
    ["Date:", "2026-02-11"],
    ["Approved by:", "K. Jha"],
    [],
    ["Designator", "Comment", "Description", "Footprint", "Manufacturer",
     "Manufacturer Part Number", "Quantity", "Supplier", "Supplier Part Number",
     "Unit Price"],
    ["R1, R2, R3, R4", "10k", "RES SMD 10K OHM 1% 1/10W 0603", "0603", "Yageo",
     "RC0603FR-0710KL", 4, "DigiKey", "311-10.0KHRCT-ND", "0.0032"],
    ["R5-R8", "100R", "RES SMD 100 OHM 1% 1/10W 0603", "0603", "Vishay",
     "CRCW0603100RFKEA", 4, "DigiKey", "541-100HCT-ND", "0.0104"],
    ["C1..C6", "100nF", "CAP CER 0.1UF 16V X7R 0603", "0603",
     "Murata Electronics", "GRM188R71C104KA01D", 6, "Mouser",
     "81-GRM188R71C104KA1D", "0.0182"],
    ["C7, C8", "10uF", "CAP CER 10UF 25V X5R 0805", "0805",
     "Murata Electronics", "GRM21BR61E106KA73L", 2, "Mouser",
     "81-GRM21BR61E106KA3L", "0.094"],
    ["U1", "LM358", "IC OPAMP GP 2 CIRCUIT SOIC-8", "SOIC-8",
     "Texas Instruments", "LM358DR", 1, "DigiKey", "296-1013-1-ND", "0.29"],
    ["U2", "MAX3232", "IC TRANSCEIVER RS232 2/2 DIP-16", "DIP-16",
     "Analog Devices", "MAX3232ECPE+", 1, "DigiKey", "MAX3232ECPE+-ND", "3.90"],
    ["U3", "MPU-6050", "SENSOR 6-AXIS IMU I2C QFN-24", "QFN-24",
     "TDK InvenSense", "MPU-6050", 1, "", "", "3.20"],
    ["U4", "STM32F103", "IC MCU 32BIT 64KB FLASH LQFP-48", "LQFP-48",
     "STMicroelectronics", "STM32F103C8T6", 1, "Mouser",
     "511-STM32F103C8T6", "2.45"],
    ["Y1", "8MHz", "CRYSTAL 8.0000MHZ 18PF SMD", "3225", "Abracon",
     "ABM8-8.000MHZ-B2-T", 1, "DigiKey", "535-9861-1-ND", "0.48"],
    ["D1, D2", "BAT54S", "DIODE SCHOTTKY 30V 200MA SOT-23", "SOT-23",
     "Nexperia", "BAT54S", 2, "DigiKey", "1727-3524-1-ND", "0.067"],
    ["Q1", "AO3400A", "MOSFET N-CH 30V 5.7A SOT-23", "SOT-23",
     "Alpha & Omega", "AO3400A", 1, "LCSC", "C20917", "0.098"],
    ["J1", "Header 4", "CONN HEADER VERT 4POS 2.54MM", "THT",
     "Wurth Elektronik", "61300411121", 1, "DigiKey", "732-5316-ND", "0.25"],
    ["F1", "2A", "FUSE BOARD MOUNT 2A 125V 1206", "1206", "Littelfuse",
     "0451002.MRL", 1, "DigiKey", "F2597CT-ND", "0.32"],
    ["MH1, MH2, MH3, MH4", "MOUNTING HOLE", "Mounting hole 3.2mm", "", "", "",
     4, "", "", ""],
    ["", "", "", "", "", "", "", "", "", ""],
    ["Total parts:", 29],
]

# --------------------------------------------------------------------------- #
# 2. Messy hand-made spreadsheet: European decimals, mixed separators,
#    an inverted "Fit?" column, a duplicate line, junk rows and a section
#    banner in the middle of the data.
# --------------------------------------------------------------------------- #

MESSY = [
    ["ACME Electronics -- Power Board BOM"],
    ["rev", "B2", "", "date", "03/02/2026"],
    [],
    ["Sl. No.", "Qty", "Part No", "Make", "Details", "Location", "Fit?",
     "Rate (EUR)", "Remarks"],
    ["--- PASSIVES ---"],
    [1, "4", "RC0603FR-0710KL", "YAGEO", "10K 1% 0603", "R1;R2;R3;R4", "Y",
     "0,0032", ""],
    [2, "2", "GRM188R71C104KA01D", "murata", "100nF 16V X7R", "C1/C2", "Y",
     "0,0182", ""],
    [3, "2", "GRM188R71C104KA01D", "Murata Mfg", "100nF 16V X7R", "C3 C4", "Y",
     "0,0182", "same as line 2"],
    [4, "1", "TAJB106K016RNJ", "AVX", "10uF 16V tant 1210", "C5", "Y", "0,43",
     "check lifecycle"],
    ["--- SEMICONDUCTORS ---"],
    [5, "1", "LM7805CT", "ON Semi", "5V 1A regulator TO-220", "U1", "Y",
     "0,58", ""],
    [6, "1", "1N4148W-7-F", "Diodes Inc", "switching diode SOD-123", "D1", "Y",
     "0,029", ""],
    [7, "2", "SS34", "ON Semiconductor", "schottky 40V 3A", "D2,D3", "Y",
     "0,15", ""],
    [8, "1", "IRLML6244TRPBF", "Infineon", "N-MOSFET 20V SOT-23", "Q1", "Y",
     "0,44", ""],
    ["--- HARDWARE ---"],
    [9, "1", "1725656", "Phoenix Contact", "terminal block 2pos", "J1", "Y",
     "1,10", ""],
    [10, "2", "TL3342F160QG", "E-Switch", "tactile switch", "SW1,SW2", "N",
     "0,13", "not fitted on rev B2"],
    [11, "1", "G5LE-14-DC12", "Omron", "relay SPDT 10A 12V", "K1", "Y", "1,68",
     ""],
    [12, "1", "", "", "heatsink -- see drawing", "HS1", "Y", "", "mechanical"],
    [],
    ["Prepared by: A. Engineer", "", "Checked: QA"],
]

# --------------------------------------------------------------------------- #
# 3. ERP extract: internal part numbers only, tab separated, no MPN column
# --------------------------------------------------------------------------- #

ERP = [
    ["MATNR", "MENGE", "MEINS", "MAKTX", "WERKS", "LGORT"],
    ["1000-4471-001", "4", "PC", "RESISTOR 10K 1% 0603", "1200", "0001"],
    ["1000-4471-002", "6", "PC", "CAPACITOR 100NF 16V 0603", "1200", "0001"],
    ["1000-8823-010", "1", "PC", "IC OPAMP DUAL SOIC8", "1200", "0001"],
    ["1000-9910-004", "1", "PC", "MCU 32BIT LQFP48", "1200", "0001"],
    ["1000-2201-007", "2", "PC", "DIODE SCHOTTKY SOT23", "1200", "0001"],
]

# --------------------------------------------------------------------------- #
# 4. A file where Excel has damaged the data
# --------------------------------------------------------------------------- #

DAMAGED = [
    ["Item", "Quantity", "MFG", "MFG P/N", "Desc", "Ref Des"],
    [1, 4, "Yageo", "RC0603FR-0710KL", "RES 10K 0603", "R1-R4"],
    [2, 2, "Murata", "1.23E+09", "CAP 100nF - number mangled by Excel", "C1,C2"],
    [3, 1, "TI", "#N/A", "IC - formula error", "U1"],
    [4, 0, "Vishay", "CRCW060310K0FKEA", "RES 10K 0603 - qty zero", "R9"],
    [5, "two", "Nexperia", "BAT54S", "DIODE - qty not numeric", "D1,D2"],
    [6, 1, "", "", "no part number at all", "U7"],
    [7, 1, "Murata", "GRM188R71C104KA01D  ", "trailing spaces", "C9"],
    [8, 3, "Yageo", "RC0603FR-0710KL", "duplicate of line 1", "R5-R7"],
    [9, 1, "TI", "LM358DR", "ref designator clash with line 3", "U1"],
]


def build_multisheet_xlsx(path: Path) -> Path:
    """A workbook with a cover sheet and two BOM sheets sharing a layout."""
    sys.path.insert(0, str(HERE.parent))
    from bomiq.export.xlsx_writer import Workbook

    wb = Workbook()
    header = wb.style(bold=True, bg="1F2937", color="FFFFFF")

    cover = wb.sheet("Cover", widths=[26, 46])
    cover.row(["Assembly", "Motor Controller"], style=wb.style(bold=True))
    cover.row(["Revision", "A"])
    cover.row(["Released", "2026-03-04"])
    cover.row(["Notes", "Two sub-assemblies, one sheet each."])

    for sheet_name, lines in (
        ("Main board", [
            ("R1-R6", 6, "Yageo", "RC0402FR-071KL", "RES 1K 1% 0402", "0402"),
            ("C1-C4", 4, "Samsung", "CL10B104KB8NNNC", "CAP 100nF 50V 0603",
             "0603"),
            ("U1", 1, "STMicroelectronics", "STM32F407VGT6",
             "MCU 32BIT 1MB LQFP-100", "LQFP-100"),
            ("U2", 1, "Microchip", "MCP2551-I/SN", "CAN TRANSCEIVER SOIC-8",
             "SOIC-8"),
            ("L1", 1, "Coilcraft", "XAL4020-222MEC", "IND 2.2UH 8.7A", "4020"),
        ]),
        ("Power board", [
            ("C10, C11", 2, "Panasonic", "EEE-FK1V101P", "CAP 100UF 35V SMD",
             "SMD"),
            ("Q1, Q2", 2, "Infineon", "IRLML6244TRPBF", "MOSFET N-CH 20V",
             "SOT-23"),
            ("D1", 1, "ON Semiconductor", "SS34", "DIODE SCHOTTKY 40V 3A",
             "DO-214AB"),
            ("F1", 1, "Littelfuse", "0451002.MRL", "FUSE 2A 125V", "1206"),
            ("J1", 1, "Phoenix Contact", "1725656", "TERM BLOCK 2POS", "THT"),
        ]),
    ):
        sheet = wb.sheet(sheet_name, freeze="A2",
                         widths=[20, 8, 24, 26, 40, 14])
        sheet.row(["Reference", "Qty", "Manufacturer",
                   "Manufacturer Part Number", "Description", "Package"],
                  style=header)
        for row in lines:
            sheet.row(list(row))

    return wb.save(path)


def build_json_bom(path: Path) -> Path:
    payload = {
        "assembly": "IoT Gateway",
        "revision": "D",
        "lines": [
            {"refs": "R1,R2", "qty": 2, "mpn": "RC0603FR-07100RL",
             "manufacturer": "Yageo", "description": "RES 100R 1% 0603",
             "package": "0603"},
            {"refs": "C1-C8", "qty": 8, "mpn": "CL10B104KB8NNNC",
             "manufacturer": "Samsung Electro-Mechanics",
             "description": "CAP CER 0.1UF 50V X7R 0603", "package": "0603"},
            {"refs": "U1", "qty": 1, "mpn": "ESP32-WROOM-32E",
             "manufacturer": "Espressif Systems",
             "description": "MODULE WIFI/BT MCU 4MB FLASH",
             "package": "SMD Module",
             "alternates": ["ESP32-WROOM-32D", "ESP32-WROOM-32UE"]},
            {"refs": "U2", "qty": 1, "mpn": "W25Q128JVSIQ",
             "manufacturer": "Winbond", "description": "FLASH 128MBIT SPI",
             "package": "SOIC-8"},
            {"refs": "U3", "qty": 1, "mpn": "TPS62840DLCR",
             "manufacturer": "Texas Instruments",
             "description": "IC REG BUCK 750MA SOT-563", "package": "SOT-563"},
            {"refs": "ANT1", "qty": 1, "mpn": "2450AT18A100E",
             "manufacturer": "Johanson Technology",
             "description": "ANTENNA CHIP 2.4GHZ", "package": "0805"},
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def main() -> int:
    written: list[Path] = []
    written.append(write_csv("01-altium-export.csv", ALTIUM))
    written.append(write_csv("02-messy-handmade.csv", MESSY,
                             encoding="cp1252"))
    written.append(write_csv("03-erp-extract.tsv", ERP, delimiter="\t"))
    written.append(write_csv("04-excel-damaged.csv", DAMAGED))
    written.append(build_multisheet_xlsx(HERE / "05-multi-sheet.xlsx"))
    written.append(build_json_bom(HERE / "06-iot-gateway.json"))

    # A semicolon-delimited variant, which is what a German Excel produces.
    semi = HERE / "07-semicolon-german.csv"
    with semi.open("w", encoding="cp1252", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["Pos", "Anzahl", "Hersteller", "Herstellernummer",
                         "Beschreibung", "Bezeichnung", "Preis"])
        writer.writerow([1, 4, "Wurth Elektronik", "150060GS75000",
                         "LED GRUEN 0603", "D1-D4", "0,12"])
        writer.writerow([2, 2, "TDK", "BLM18PG221SN1D", "FERRITE 220 OHM 0603",
                         "FB1;FB2", "0,053"])
        writer.writerow([3, 1, "Rohm", "SN74HC595N", "SCHIEBEREGISTER DIP-16",
                         "U1", "0,52"])
    written.append(semi)

    print("Wrote:")
    for path in written:
        print(f"  {path.name:28s} {path.stat().st_size:>8,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
