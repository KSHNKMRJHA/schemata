"""Engineering risk score model (SVG section 6).

Weights default to the SVG example profile and are tunable in config.toml
[risk]. Each component is normalized to 0..1 then combined as a weighted sum.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import config_section
from app.history import History
from app.lifecycle import status_risk
from app.models import Component, LifecycleStatus


@dataclass
class RiskResult:
    score: int
    level: str
    breakdown: dict  # metric name -> 0..1 normalized sub-risk
    weighted: dict  # metric name -> contribution to final score
    notes: list[str] = field(default_factory=list)

    @property
    def is_critical(self) -> bool:
        return self.level in ("HIGH", "CRITICAL")


def _weights() -> dict[str, float]:
    cfg = config_section("risk")
    return {
        "lifecycle": float(cfg.get("weight_lifecycle", 40)),
        "stock": float(cfg.get("weight_stock", 20)),
        "lead_time": float(cfg.get("weight_lead_time", 15)),
        "vendor_diversity": float(cfg.get("weight_vendor_diversity", 10)),
        "price_trend": float(cfg.get("weight_price_trend", 10)),
        "manufacturer": float(cfg.get("weight_manufacturer", 5)),
    }


def level_for(score: int) -> str:
    if score < 25:
        return "LOW"
    if score < 50:
        return "MEDIUM"
    if score < 75:
        return "HIGH"
    return "CRITICAL"


def assess(component: Component, history: History, live_sources: list[str]) -> RiskResult:
    weights = _weights()

    # 1. Lifecycle status.
    lifecycle_risk = status_risk(component.lifecycle_status)

    # 2 & 3. Stock availability + lead time across current snapshots.
    snaps = component.snapshots
    current = [s for s in snaps if _is_recent(s)]
    if current:
        sum_stock = sum(s.available_qty or 0 for s in current if s.available)
        stock_risk = 1.0 - min(1.0, sum_stock / 1000.0)
        lead_risk = _lead_time_risk(current)
    else:
        stock_risk = 0.5 if snaps else 0.8
        lead_risk = 0.5 if snaps else 0.9

    # 4. Vendor diversity: more live/distinct stock-holding vendors = lower risk.
    vendors = {s.distributor for s in current if s.available}
    diversity_risk = 0.0 if len(vendors) >= 3 else (0.4 if len(vendors) == 2 else (0.7 if len(vendors) == 1 else 1.0))

    # 5. Price trend.
    price_risk = 0.9 if history.price_trend_up else (0.2 if history.price_delta_pct is not None else 0.5)

    # 6. Manufacturer attribution quality.
    mfr_risk = 0.0 if component.manufacturer else 1.0

    breakdown = {
        "lifecycle": lifecycle_risk,
        "stock": stock_risk,
        "lead_time": lead_risk,
        "vendor_diversity": diversity_risk,
        "price_trend": price_risk,
        "manufacturer": mfr_risk,
    }
    total_weight = sum(weights.values()) or 100
    score = round(sum(breakdown[k] * weights[k] for k in breakdown) / total_weight * 100)
    score = max(0, min(100, score))

    notes: list[str] = []
    eolish = (
        LifecycleStatus.NRND.value,
        LifecycleStatus.EOL_ANNOUNCED.value,
        LifecycleStatus.LAST_TIME_BUY.value,
    )
    if component.lifecycle_status in eolish:
        notes.append(f"Lifecycle status '{component.lifecycle_status}' limits new-design suitability.")
    if stock_risk > 0.75:
        notes.append("Very low or zero aggregate stock across vendors.")
    if lead_risk > 0.6:
        notes.append("Long or unknown lead times present.")
    if diversity_risk >= 0.7:
        notes.append("Supply relies on fewer than two stock-holding vendors.")
    if component.lifecycle_status == LifecycleStatus.UNKNOWN.value:
        notes.append("Lifecycle status unverified — treat as risk until confirmed.")

    return RiskResult(
        score=score,
        level=level_for(score),
        breakdown=breakdown,
        weighted={k: round(breakdown[k] * weights[k], 1) for k in breakdown},
        notes=notes,
    )


def _is_recent(snap) -> bool:
    from datetime import timedelta

    from app.util import utcnow

    cfg = config_section("cache")
    ttl = timedelta(minutes=float(cfg.get("snapshot_ttl_minutes", 15)))
    age = utcnow() - snap.checked_at.replace(tzinfo=None)
    return age <= ttl


def _lead_time_risk(snaps) -> float:
    weeks: list[float] = []
    for s in snaps:
        num = _weeks(s.lead_time)
        if num is not None:
            weeks.append(num)
    if not weeks:
        return 0.8
    avg = sum(weeks) / len(weeks)
    return min(1.0, avg / 52.0)


def _weeks(text: str) -> float | None:
    """Parse '8 Weeks', '10w', '3 days', 'In Stock' into weeks (None if unknown)."""
    import re

    t = (text or "").lower().strip()
    if not t or "in stock" in t or "stock" in t:
        return None
    m = re.search(r"\d+(?:\.\d+)?", t)
    if m:
        num = float(m.group(0))
    else:
        return None
    if "day" in t or "d " in t or t.endswith("d"):
        return round(num / 7, 2)
    return num
