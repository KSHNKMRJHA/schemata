"""Demo fixture catalog used by stub adapters and seed_demo.

Every value here is plausible-but-synthetic demo data. Real adapters
(Mouser / DigiKey) replace per-part facts when keys are configured.
Distributor stock/price figures deliberately mirror the SVG mockups.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DemoOffer:
    stock: int
    lead: str
    breaks: list  # [(qty, price)]
    url: str = ""


@dataclass
class DemoPart:
    mpn: str
    manufacturer: str
    family: str
    category: str
    description: str
    package: str
    lifecycle: str
    rohs: str = "Yes"
    reach: str = "Compliant"
    voltage: str = ""
    current: str = ""
    frequency: str = ""
    temperature: str = ""
    datasheet: str = ""
    product_url: str = ""
    notices: list = field(default_factory=list)  # (event_type, date_iso, notes)
    offers: dict = field(default_factory=dict)  # distributor -> DemoOffer


def _o(d: dict) -> dict:
    stock, lead, breaks, url = d.get("stock", 0), d.get("lead", ""), d.get("breaks", []), d.get("url", "")
    return {"stock": stock, "lead": lead, "breaks": breaks, "url": url}


CATALOG: dict[str, DemoPart] = {}


def _reg(p: DemoPart) -> None:
    CATALOG[p.mpn] = p


_MSR = "https://www.mouser.com/ProductDetail/"
_DKY = "https://www.digikey.com/en/products/detail/"
_FNL = "https://uk.farnell.com/productsearch"


def _mouser_lead(mpn: str) -> str:
    return "8 weeks"


def _common_offers(mpn: str, mouser_stock, digi_stock, farnell_stock, unit_price, mouser_url, digi_url, farnell_url):
    breaks_m = [
        (1, unit_price),
        (10, round(unit_price * 0.94, 2)),
        (100, round(unit_price * 0.88, 2)),
        (250, round(unit_price * 0.82, 2)),
        (500, round(unit_price * 0.79, 2)),
    ]
    breaks_d = [
        (1, round(unit_price * 0.97, 2)),
        (10, round(unit_price * 0.92, 2)),
        (100, round(unit_price * 0.85, 2)),
        (250, round(unit_price * 0.8, 2)),
        (500, round(unit_price * 0.76, 2)),
    ]
    breaks_f = [(1, round(unit_price * 1.02, 2)), (10, round(unit_price * 0.96, 2)), (100, round(unit_price * 0.9, 2))]
    return {
        "Mouser": _o({"stock": mouser_stock, "lead": _mouser_lead(mpn), "breaks": breaks_m, "url": mouser_url}),
        "DigiKey": _o({"stock": digi_stock, "lead": "10 weeks", "breaks": breaks_d, "url": digi_url}),
        "Farnell": _o({"stock": farnell_stock, "lead": "6 weeks", "breaks": breaks_f, "url": farnell_url}),
    }


# ---- Microcontrollers / processors ----
_reg(
    DemoPart(
        mpn="STM32F407VGT6",
        manufacturer="STMicroelectronics",
        family="STM32F4 Series",
        category="Microcontrollers",
        description="High-performance MCU, ARM Cortex-M4F 168MHz, 1MB Flash, 192KB RAM, Ethernet/MAC/CAN",
        package="LQFP100",
        lifecycle="ACTIVE",
        voltage="1.8-3.6V",
        current="100mA @ 168MHz",
        frequency="168MHz",
        temperature="-40 to +85 C",
        datasheet="https://www.st.com/resource/en/datasheet/stm32f407vg.pdf",
        product_url="https://www.st.com/en/microcontrollers-microprocessors/stm32f407vg.html",
        offers=_common_offers(
            "STM32F407VGT6",
            1247,
            3821,
            642,
            42.29,
            _MSR + "mouser/STM32F407VGT6",
            _DKY + "stm32f407vg0/stm32f407vgt6",
            _FNL + "STM32F407VGT6",
        ),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="STM32F103C8T6",
        manufacturer="STMicroelectronics",
        family="STM32F1 Series",
        category="Microcontrollers",
        description="Mid-density MCU, ARM Cortex-M3 72MHz, 64KB Flash, 20KB RAM (classic Blue Pill core)",
        package="LQFP48",
        lifecycle="NRND",
        voltage="2.0-3.6V",
        current="36mA @ 72MHz",
        frequency="72MHz",
        temperature="-40 to +85 C",
        datasheet="https://www.st.com/resource/en/datasheet/stm32f103c8.pdf",
        product_url="https://www.st.com/en/microcontrollers-microprocessors/stm32f103c8.html",
        notices=[
            ("EOL_ANNOUNCED", "2023-06-30", "ST moved F1 series to NRND; successors are STM32G4 / STM32C0 families.")
        ],
        offers=_common_offers("STM32F103C8T6", 8120, 15400, 2210, 2.68, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="STM32H743VIT6",
        manufacturer="STMicroelectronics",
        family="STM32H7 Series",
        category="Microcontrollers",
        description="High-performance MCU, Cortex-M7 480MHz, 2MB Flash, 1MB RAM, TFT/JPEG/H264",
        package="LQFP100",
        lifecycle="ACTIVE",
        voltage="1.62-3.6V",
        current="280mA @ 480MHz",
        frequency="480MHz",
        temperature="-40 to +125 C",
        datasheet="https://www.st.com/resource/en/datasheet/stm32h743vi.pdf",
        product_url="https://www.st.com/en/microcontrollers-microprocessors/stm32h743vi.html",
        offers=_common_offers("STM32H743VIT6", 340, 890, 110, 18.51, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="ATMEGA328P-PU",
        manufacturer="Microchip",
        family="AVR",
        category="Microcontrollers",
        description="8-bit AVR MCU, 20MHz, 32KB Flash, 2KB SRAM, 1KB EEPROM (Arduino Uno core), DIP-28",
        package="DIP28",
        lifecycle="ACTIVE",
        voltage="1.8-5.5V",
        current="1.5mA @ 3V",
        frequency="20MHz",
        temperature="-40 to +85 C",
        datasheet="https://ww1.microchip.com/downloads/en/DeviceDoc/Atmel-7810-Automotive-Microcontrollers-ATmega328P_Datasheet.pdf",
        product_url="https://www.microchip.com/en-us/product/atmega328p",
        offers=_common_offers("ATMEGA328P-PU", 3300, 7400, 1200, 2.19, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="ESP32-WROOM-32E",
        manufacturer="Espressif",
        family="ESP32",
        category="Wireless Modules",
        description="WiFi/BT MCU module, dual-core Xtensa LX6 240MHz, 4MB flash, PCB antenna",
        package="Module-SMD",
        lifecycle="ACTIVE",
        voltage="2.3-3.6V",
        current="240mA TX",
        frequency="2.4GHz",
        temperature="-40 to +85 C",
        datasheet="https://www.espressif.com/sites/default/files/documentation/esp32-wroom-32e_esp32-wroom-32ue_datasheet_en.pdf",
        product_url="https://www.espressif.com/en/products/modules/esp32-wroom-32e",
        offers=_common_offers("ESP32-WROOM-32E", 4100, 9800, 980, 2.15, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="NRF51822-QFAA",
        manufacturer="Nordic Semiconductor",
        family="nRF51",
        category="Wireless SoCs",
        description="BLE SoC, Cortex-M0 16MHz, 256KB Flash, 16KB RAM",
        package="QFN48",
        lifecycle="NRND",
        voltage="1.8-3.6V",
        current="9.8mA RX",
        frequency="2.4GHz",
        temperature="-40 to +85 C",
        datasheet="https://infocenter.nordicsemi.com/pdf/nRF51822_OPS_Spec_v3.4.pdf",
        product_url="https://www.nordicsemi.com/Products/nRF51822",
        notices=[("EOL_ANNOUNCED", "2022-12-15", "nRF51822 phased out in favour of nRF52 series.")],
        offers=_common_offers("NRF51822-QFAA", 430, 1250, 0, 3.41, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="MSP430G2553",
        manufacturer="Texas Instruments",
        family="MSP430",
        category="Microcontrollers",
        description="Ultra-low-power MCU, 16MHz, 16KB Flash, 512B RAM, mixed-signal peripherals",
        package="PDIP20",
        lifecycle="ACTIVE",
        voltage="1.8-3.6V",
        current="200uA @ 1MHz",
        frequency="16MHz",
        temperature="-40 to +85 C",
        datasheet="https://www.ti.com/lit/ds/symlink/msp430g2553.pdf",
        product_url="https://www.ti.com/product/MSP430G2553",
        offers=_common_offers("MSP430G2553", 5600, 12200, 2100, 1.84, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

# ---- Analog / power ----
_reg(
    DemoPart(
        mpn="LM7805CT",
        manufacturer="Texas Instruments",
        family="78xx",
        category="Linear Regulators",
        description="Fixed 5V 1.5A positive voltage regulator, TO-220",
        package="TO220",
        lifecycle="LAST_TIME_BUY",
        voltage="7-25V IN / 5V OUT",
        current="1.5A",
        frequency="",
        temperature="0 to +125 C",
        datasheet="https://www.ti.com/lit/ds/symlink/lm340.pdf",
        product_url="https://www.ti.com/product/LM340",
        notices=[
            ("EOL_ANNOUNCED", "2023-09-01", "TI announced EOL for classic 78xx TO-220 variants."),
            (
                "LAST_TIME_BUY",
                "2024-03-15",
                "Final orders accepted until mid-2024; recommend LDO or switcher alternates.",
            ),
        ],
        offers=_common_offers("LM7805CT", 1800, 420, 90, 0.79, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="LT1117CT",
        manufacturer="Analog Devices",
        family="LT1117",
        category="Linear Regulators",
        description="Low dropout fixed 3.3V 0.8A linear regulator, TO-220",
        package="TO220",
        lifecycle="EOL_ANNOUNCED",
        voltage="4.75-15V IN / 3.3V OUT",
        current="0.8A",
        frequency="",
        temperature="-40 to +125 C",
        datasheet="https://www.analog.com/media/en/technical-documentation/data-sheets/1117fb.pdf",
        product_url="https://www.analog.com/en/products/lt1117.html",
        notices=[
            ("EOL_ANNOUNCED", "2024-06-01", "ADI announced end-of-life; LTB window open."),
            ("SUCCESSOR", "2024-06-01", "Recommended successor ADM7150 / LD1117 family."),
        ],
        offers=_common_offers("LT1117CT", 220, 640, 0, 3.92, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="TPS54331DR",
        manufacturer="Texas Instruments",
        family="TPS54",
        category="Switching Regulators",
        description="3.5-28V input, 3A buck converter, internal FET, SOIC-8",
        package="SOIC8",
        lifecycle="ACTIVE",
        voltage="3.5-28V",
        current="3A",
        frequency="570kHz",
        temperature="-40 to +125 C",
        datasheet="https://www.ti.com/lit/ds/symlink/tps54331.pdf",
        product_url="https://www.ti.com/product/TPS54331",
        offers=_common_offers("TPS54331DR", 1900, 5200, 300, 1.47, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="LM317T",
        manufacturer="STMicroelectronics",
        family="LM317",
        category="Linear Regulators",
        description="Adjustable 1.25-37V 1.5A positive regulator, TO-220",
        package="TO220",
        lifecycle="ACTIVE",
        voltage="3-40V",
        current="1.5A",
        frequency="",
        temperature="0 to +125 C",
        datasheet="https://www.st.com/resource/en/datasheet/lm317.pdf",
        product_url="https://www.st.com/en/linear-regulators/lm317.html",
        offers=_common_offers("LM317T", 3100, 8100, 1900, 0.62, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="NE5532P",
        manufacturer="Texas Instruments",
        family="NE5532",
        category="Op Amps",
        description="Dual low-noise op amp, 10MHz, 9V/us, DIP-8",
        package="DIP8",
        lifecycle="ACTIVE",
        voltage="3-32V",
        current="8mA",
        frequency="10MHz",
        temperature="-40 to +85 C",
        datasheet="https://www.ti.com/lit/ds/symlink/ne5532.pdf",
        product_url="https://www.ti.com/product/NE5532",
        offers=_common_offers("NE5532P", 4200, 11800, 2500, 0.68, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="OPA2134PA",
        manufacturer="Texas Instruments",
        family="OPA2134",
        category="Op Amps",
        description="SoundPlus dual audio op amp, 8MHz, 20V/us, low distortion, DIP-8",
        package="DIP8",
        lifecycle="ACTIVE",
        voltage="2.5-18V",
        current="4mA",
        frequency="8MHz",
        temperature="0 to +70 C",
        datasheet="https://www.ti.com/lit/ds/symlink/opa2134.pdf",
        product_url="https://www.ti.com/product/OPA2134",
        offers=_common_offers("OPA2134PA", 640, 1800, 210, 3.12, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="MAX232CPE",
        manufacturer="Analog Devices",
        family="MAX232",
        category="Interface ICs",
        description="Dual RS-232 driver/receiver, 5V, DIP-16",
        package="DIP16",
        lifecycle="LAST_TIME_BUY",
        voltage="4.5-5.5V",
        current="8mA",
        frequency="120kbps",
        temperature="0 to +70 C",
        datasheet="https://www.analog.com/media/en/technical-documentation/data-sheets/max232.pdf",
        product_url="https://www.analog.com/en/products/max232.html",
        notices=[
            ("EOL_ANNOUNCED", "2023-11-01", "ADI ending MAX232 through-hole family."),
            ("LAST_TIME_BUY", "2024-05-31", "Final buy window closing; prefer MAX3232 for 3.3V systems."),
        ],
        offers=_common_offers("MAX232CPE", 190, 760, 140, 2.84, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="CD4051BM",
        manufacturer="Texas Instruments",
        family="CD4051",
        category="Analog Switches",
        description="8-channel analog multiplexer/demultiplexer, SOIC-16",
        package="SOIC16",
        lifecycle="NRND",
        voltage="3-18V",
        current="0.3mA",
        frequency="1MHz",
        temperature="-55 to +125 C",
        datasheet="https://www.ti.com/lit/ds/symlink/cd4051b.pdf",
        product_url="https://www.ti.com/product/CD4051B",
        notices=[("EOL_ANNOUNCED", "2024-01-15", "TI announced EOL for CD40xx SOIC variants.")],
        offers=_common_offers("CD4051BM", 1500, 3900, 0, 0.55, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="ADS1115",
        manufacturer="Texas Instruments",
        family="ADS",
        category="ADCs",
        description="16-bit 860SPS 4-channel I2C ADC with PGA, reference and comparator.",
        package="VSSOP10",
        lifecycle="ACTIVE",
        voltage="2-5.5V",
        current="0.15mA",
        frequency="100kHz",
        temperature="-40 to +125 C",
        datasheet="https://www.ti.com/lit/ds/symlink/ads1115.pdf",
        product_url="https://www.ti.com/product/ADS1115",
        offers=_common_offers("ADS1115", 2200, 6900, 510, 3.05, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

# ---- Logic / discrete ----
_reg(
    DemoPart(
        mpn="74HC595D",
        manufacturer="Nexperia",
        family="74HC",
        category="Shift Registers",
        description="8-bit serial-in/parallel-out shift register with latched outputs, SOIC-16",
        package="SOIC16",
        lifecycle="ACTIVE",
        voltage="2-6V",
        current="5.2mA",
        frequency="100MHz",
        temperature="-40 to +125 C",
        datasheet="https://www.nexperia.com/documents/data-sheet/74HC_HCT595.pdf",
        product_url="https://www.nexperia.com/products/analog-logic-ics/standard-logic/shift-registers/series-74hc#/products",
        offers=_common_offers("74HC595D", 5200, 14900, 3300, 0.14, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="74LVC1G17",
        manufacturer="Nexperia",
        family="74LVC",
        category="Buffers",
        description="Single Schmitt-trigger buffer, SOT23-5, 1.65-5.5V",
        package="SOT23-5",
        lifecycle="ACTIVE",
        voltage="1.65-5.5V",
        current="0.001mA",
        frequency="150MHz",
        temperature="-40 to +125 C",
        datasheet="https://www.nexperia.com/documents/data-sheet/74LVC1G17.pdf",
        product_url="https://www.nexperia.com/products/analog-logic-ics/standard-logic/buffers-inverters/74lvc1g17",
        offers=_common_offers("74LVC1G17", 11000, 28700, 6100, 0.12, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="BC547B",
        manufacturer="Nexperia",
        family="BC",
        category="BJT Transistors",
        description="NPN 45V 100mA 45MHz small-signal transistor, TO-92",
        package="TO92",
        lifecycle="ACTIVE",
        voltage="45V",
        current="100mA",
        frequency="45MHz",
        temperature="-65 to +150 C",
        datasheet="https://www.nexperia.com/documents/data-sheet/BC546_547_548_549_550.pdf",
        product_url="https://www.nexperia.com/products/transistors/bipolar-transistors/bc547",
        offers=_common_offers("BC547B", 22000, 41800, 9800, 0.03, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="2N7002",
        manufacturer="Nexperia",
        family="2N",
        category="MOSFETs",
        description="N-channel 60V 0.3A enhancement MOSFET, SOT-23",
        package="SOT23",
        lifecycle="ACTIVE",
        voltage="60V",
        current="0.3A",
        frequency="",
        temperature="-55 to +150 C",
        datasheet="https://www.nexperia.com/documents/data-sheet/2N7002.pdf",
        product_url="https://www.nexperia.com/products/mosfets/2n7002",
        offers=_common_offers("2N7002", 18000, 39500, 8200, 0.05, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="IRF540N",
        manufacturer="Infineon",
        family="IRF",
        category="MOSFETs",
        description="N-channel 100V 33A power MOSFET, TO-220",
        package="TO220",
        lifecycle="ACTIVE",
        voltage="100V",
        current="33A",
        frequency="",
        temperature="-55 to +175 C",
        datasheet="https://www.infineon.com/dgdl/Infineon-IRF540N-DataSheet-v01_01-EN.pdf",
        product_url="https://www.infineon.com/cms/en/product/power/mosfet/n-channel/irf540n/",
        offers=_common_offers("IRF540N", 2900, 8300, 1400, 0.78, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="STP55NF06L",
        manufacturer="STMicroelectronics",
        family="STP",
        category="MOSFETs",
        description="N-channel 60V 55A STripFET power MOSFET, TO-220",
        package="TO220",
        lifecycle="ACTIVE",
        voltage="60V",
        current="55A",
        frequency="",
        temperature="-55 to +175 C",
        datasheet="https://www.st.com/resource/en/datasheet/stp55nf06l.pdf",
        product_url="https://www.st.com/en/power-transistors/stp55nf06l.html",
        offers=_common_offers("STP55NF06L", 1300, 3600, 420, 0.91, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

# ---- Interface / comms ----
_reg(
    DemoPart(
        mpn="MCP2515-I/ST",
        manufacturer="Microchip",
        family="MCP2515",
        category="CAN Controllers",
        description="Stand-alone SPI CAN controller with message filtering, TSSOP-20",
        package="TSSOP20",
        lifecycle="ACTIVE",
        voltage="2.7-5.5V",
        current="10mA",
        frequency="1Mbps",
        temperature="-40 to +85 C",
        datasheet="https://ww1.microchip.com/downloads/en/DeviceDoc/MCP2515-Stand-Alone-CAN-Controller-20001801J.pdf",
        product_url="https://www.microchip.com/en-us/product/MCP2515",
        offers=_common_offers("MCP2515-I/ST", 3400, 9800, 1100, 1.42, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="TJA1050",
        manufacturer="NXP",
        family="TJA",
        category="CAN Transceivers",
        description="High-speed CAN transceiver, 1Mbps, SOIC-8",
        package="SOIC8",
        lifecycle="ACTIVE",
        voltage="4.75-5.25V",
        current="60mA",
        frequency="1Mbps",
        temperature="-40 to +125 C",
        datasheet="https://www.nxp.com/docs/en/data-sheet/TJA1050.pdf",
        product_url="https://www.nxp.com/products/interfaces/can-bus/can-transceivers:MC_CAN-TRANSCEIVERS",
        offers=_common_offers("TJA1050", 6100, 14700, 7300, 0.62, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="CP2102N-A02-GQFN28R",
        manufacturer="Silicon Labs",
        family="CP210x",
        category="USB Bridges",
        description="Single-chip USB-to-UART bridge with 10 pin package, QFN-24",
        package="QFN24",
        lifecycle="ACTIVE",
        voltage="1.8-3.6V",
        current="8.5mA",
        frequency="3Mbps",
        temperature="-40 to +85 C",
        datasheet="https://www.silabs.com/documents/public/data-sheets/cp2102n-datasheet.pdf",
        product_url="https://www.silabs.com/interface/usb-bridges/classic-usb-bridges/device.cp2102n",
        offers=_common_offers("CP2102N-A02-GQFN28R", 4800, 13200, 0, 2.86, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="IC-20887-QFN24",
        manufacturer="WCH",
        family="CH340",
        category="USB Bridges",
        description="USB-to-UART converter for serial communication, QFN-24 (compatible form)",
        package="QFN24",
        lifecycle="ACTIVE",
        voltage="4.5-5.5V",
        current="18mA",
        frequency="2Mbps",
        temperature="-20 to +85 C",
        datasheet="https://www.wch-ic.com/downloads/CH340DS1_PDF.html",
        product_url="https://www.wch-ic.com/products/CH340.html",
        offers={
            "Mouser": _o({"stock": 0, "lead": "", "breaks": [], "url": _MSR + "x"}),
            "DigiKey": _o({"stock": 0, "lead": "", "breaks": [], "url": _DKY + "x"}),
            "LCSC": _o(
                {
                    "stock": 89000,
                    "lead": "1 day",
                    "breaks": [(1, 0.35), (10, 0.31), (100, 0.27)],
                    "url": "https://lcsc.com",
                }
            ),
        },
    )
)  # type: ignore[misc]

# A couple more around discontinued/obsolete for BOM demo variety
_reg(
    DemoPart(
        mpn="ATtiny85-20PU",
        manufacturer="Microchip",
        family="AVR",
        category="Microcontrollers",
        description="8-bit AVR MCU, 20MHz, 8KB Flash, 512B SRAM, DIP-8",
        package="DIP8",
        lifecycle="ACTIVE",
        voltage="2.7-5.5V",
        current="0.2mA",
        frequency="20MHz",
        temperature="-40 to +85 C",
        datasheet="https://www.microchip.com/wwwproducts/en/ATTINY85-20PU",
        product_url="https://www.microchip.com/en-us/product/attiny85",
        offers=_common_offers("ATtiny85-20PU", 7600, 18900, 2700, 1.01, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]

_reg(
    DemoPart(
        mpn="LM358N",
        manufacturer="Texas Instruments",
        family="LM358",
        category="Op Amps",
        description="Dual operational amplifier, 700kHz, DIP-8",
        package="DIP8",
        lifecycle="ACTIVE",
        voltage="3-32V",
        current="0.7mA",
        frequency="700kHz",
        temperature="-40 to +85 C",
        datasheet="https://www.ti.com/lit/ds/symlink/lm358.pdf",
        product_url="https://www.ti.com/product/LM358",
        offers=_common_offers("LM358N", 9800, 20600, 5400, 0.31, _MSR + "x", _DKY + "x", _FNL + "x"),
    )
)  # type: ignore[misc]


def get(mpn: str) -> DemoPart | None:
    return CATALOG.get(mpn.upper())


def all_mpns() -> list[str]:
    return sorted(CATALOG)
