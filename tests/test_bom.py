import io

import pytest

from app.bom import _header_match, _read_csv, _row_to_fields, analyze_rows


def test_header_matching():
    assert _header_match("MPN") == "mpn"
    assert _header_match("RefDes") == "reference"
    assert _header_match("Qty Per Board") == "qty"
    assert _header_match("Target Price") == "target_price"
    assert _header_match("Notes") is None


def test_row_field_extraction():
    row = {"RefDes": "U1", "MPN": "STM32F407VGT6", "Manufacturer": "ST", "Qty": "2", "Target": "40.0"}
    fields = _row_to_fields(row)
    assert fields["reference"] == "U1"
    assert fields["mpn"] == "STM32F407VGT6"
    assert fields["manufacturer"] == "ST"
    assert fields["qty"] == "2"
    assert fields["target_price"] == "40.0"


def test_csv_reading():
    data = io.BytesIO("RefDes,MPN,Manufacturer,Qty\nU1,STM32F407VGT6,ST,1\n".encode("utf-8-sig"))
    rows = _read_csv(data)
    assert rows[0]["MPN"] == "STM32F407VGT6"


@pytest.mark.asyncio
async def test_analyze_rows_end_to_end():
    rows = [
        {"RefDes": "U1", "MPN": "STM32F407VGT6", "Manufacturer": "ST", "Qty": "2", "Target": "45.0"},
        {"RefDes": "U2", "MPN": "NO-SUCH-PART", "Manufacturer": "", "Qty": "1"},
        {"RefDes": "U3", "MPN": "LM7805CT", "Manufacturer": "TI", "Qty": "5"},
    ]
    report = await analyze_rows(rows)
    assert report.ok_lines == 2
    assert report.error_lines == 1
    assert report.lifecycle_distribution()["ACTIVE"] == 1
    assert report.lifecycle_distribution()["LAST_TIME_BUY"] == 1
    assert report.total_cost_estimate is not None
    by_mpn = {ln.mpn.upper(): ln for ln in report.lines}
    # LTB part must score strictly riskier than the active part.
    ltb, stm = by_mpn["LM7805CT"], by_mpn["STM32F407VGT6"]
    assert ltb.risk.score > stm.risk.score
    assert ltb.risk.breakdown["lifecycle"] >= 0.8
    for line in report.lines:
        if line.report:
            assert line.report.alternatives  # candidates are populated from catalog
