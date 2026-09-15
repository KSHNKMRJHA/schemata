from app.normalizer import guess_manufacturer, normalize, normalize_manufacturer_name


def test_cleans_dirty_input():
    n = normalize("  stm32f407vgt6  ")
    assert n.mpn_normalized == "STM32F407VGT6"
    assert n.mpn_raw == "stm32f407vgt6"


def test_detects_manufacturer():
    assert guess_manufacturer("STM32F407VGT6") == "STMicroelectronics"
    assert guess_manufacturer("ATMEGA328P-PU") == "Microchip"
    assert guess_manufacturer("ESP32-WROOM-32E") == "Espressif"
    assert guess_manufacturer("ZZZZ-XYZ") == ""


def test_manufacturer_aliases():
    assert normalize_manufacturer_name("TI") == "Texas Instruments"
    assert normalize_manufacturer_name("STMICRO") == "STMicroelectronics"


def test_package_suffix_hint():
    n = normalize("STM32F407VGT6")
    assert n.package_hint == "LQFP100"


def test_manual_manufacturer_wins():
    n = normalize("STM32F103C8T6", manufacturer="STMicroelectronics")
    assert n.manufacturer_hint == "STMicroelectronics"
