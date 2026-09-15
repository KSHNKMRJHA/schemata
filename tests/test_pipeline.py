import pytest

from app.service import get_part_report


@pytest.mark.asyncio
async def test_full_search_pipeline_for_stm32f4():
    report = await get_part_report("STM32F407VGT6")
    c = report.component
    assert c.mpn_normalized == "STM32F407VGT6"
    assert c.manufacturer == "STMicroelectronics"
    assert c.lifecycle_status == "ACTIVE"
    assert c.snapshots  # distributor snapshots present
    assert c.evidences  # evidence trail present
    assert any(e.status in ("verified", "unknown") for e in c.evidences)
    assert report.alternatives  # candidates scored
    assert report.risk.score >= 0
    assert report.confidence > 0


@pytest.mark.asyncio
async def test_part_unknown_is_returned_not_invented():
    report = await get_part_report("ZZ-UNKNOWN-PART-9")
    assert report.component.mpn_normalized == "ZZ-UNKNOWN-PART-9"
    assert report.component.manufacturer == ""
    assert report.component.lifecycle_status == "UNKNOWN"
    assert not report.component.snapshots
    assert report.component.evidences  # every source says "no data" — evidenced, not invented


@pytest.mark.asyncio
async def test_refresh_is_cached_and_idempotent():
    first = await get_part_report("ATMEGA328P-PU")
    count_before = len(first.component.snapshots)
    second = await get_part_report("ATMEGA328P-PU")
    # fresh cache -> no new fetch, snapshots unchanged
    assert len(second.component.snapshots) == count_before
    assert first.component.lifecycle_status == second.component.lifecycle_status
