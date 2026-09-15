from datetime import timedelta

from app import risk
from app.models import AvailabilitySnapshot, Component, LifecycleStatus
from app.util import utcnow


def _snap(vendor, qty, lead, price, checked=None):
    s = AvailabilitySnapshot()
    s.distributor = vendor
    s.available_qty = qty
    s.available = qty > 0
    s.price_breaks = [{"qty": 1, "price": price, "currency": "USD"}]
    s.lead_time = lead
    s.checked_at = checked or utcnow()
    return s


def _component(status=LifecycleStatus.ACTIVE.value, snapshots=None, manufacturer="STMicroelectronics"):
    c = Component(mpn_normalized="X", lifecycle_status=status, manufacturer=manufacturer)
    for s in snapshots or []:
        s.component_id = 1
        c.snapshots.append(s)
    return c


def test_low_risk_active_part():
    snaps = [
        _snap("Mouser", 5000, "2 weeks", 10.0),
        _snap("DigiKey", 9000, "3 weeks", 9.8),
        _snap("Farnell", 2000, "10 days", 10.2),
    ]
    c = _component(snapshots=snaps)
    r = risk.assess(c, risk.History(), [])
    assert r.score < 30
    assert r.level in ("LOW", "MEDIUM")


def test_obsolete_part_is_critical():
    c = _component(status=LifecycleStatus.OBSOLETE.value)
    r = risk.assess(c, risk.History(), [])
    assert r.score > 60
    assert r.level in ("HIGH", "CRITICAL")


def test_no_vendors_raises_stock_and_diversity_risk():
    c = _component()
    r = risk.assess(c, risk.History(), [])
    assert r.breakdown["stock"] >= 0.5
    assert r.breakdown["vendor_diversity"] == 1.0


def test_price_surge_moves_the_needle():
    base = utcnow() - timedelta(days=10)
    snaps = [
        _snap("Mouser", 100, "4 weeks", 10.0, checked=base),
        _snap("Mouser", 80, "4 weeks", 15.0, checked=utcnow()),
    ]
    c = _component(snapshots=snaps)
    from app.history import build_history

    r = risk.assess(c, build_history(c.snapshots), [])
    assert r.breakdown["price_trend"] > 0.5
