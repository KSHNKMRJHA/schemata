from app.alternatives import rank_alternatives, score_candidate
from app.models import LifecycleStatus

TARGET = {
    "mpn_normalized": "STM32F407VGT6",
    "manufacturer": "STMicroelectronics",
    "category": "Microcontrollers",
    "description": "ARM Cortex-M4F MCU",
    "package": "LQFP100",
    "lifecycle_status": LifecycleStatus.ACTIVE.value,
    "operating_voltage": "1.8-3.6V",
    "current": "100mA",
    "frequency": "168MHz",
    "temperature": "-40 to +85 C",
    "available": True,
}


def _cand(mpn, package, category="Microcontrollers", lifecycle="ACTIVE", voltage="1.62-3.6V", avail=True):
    return {
        "mpn": mpn,
        "manufacturer": "X",
        "category": category,
        "description": "",
        "package": package,
        "lifecycle_status": lifecycle,
        "operating_voltage": voltage,
        "current": "",
        "frequency": "",
        "temperature": "",
        "available": avail,
    }


def test_same_package_same_category_is_drop_in():
    s = score_candidate(TARGET, _cand("STM32F407VGT7", "LQFP100"))
    assert s.kind == "DROP_IN"
    assert s.score > 0.5


def test_different_package_is_functional():
    s = score_candidate(TARGET, _cand("STM32F407VET6", "LQFP64"))
    assert s.kind == "FUNCTIONAL"


def test_unrelated_category_is_parametric_or_weak():
    s = score_candidate(TARGET, _cand("NE5532P", "DIP8", category="Op Amps"))
    assert s.kind in ("PARAMETRIC", "FUNCTIONAL")
    assert s.score < 0.5


def test_identity_is_excluded():
    s = score_candidate(TARGET, _cand("STM32F407VGT6", "LQFP100"))
    assert s.score == 0.0


def test_rank_orders_drop_in_first():
    candidates = [
        _cand("NE5532P", "DIP8", category="Op Amps"),
        _cand("STM32F407VGT7", "LQFP100"),
        _cand("STM32H743VIT6", "LQFP100"),
    ]
    ranked = rank_alternatives(TARGET, candidates)
    assert ranked[0].kind == "DROP_IN"
