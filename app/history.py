"""Price / availability history: snapshots -> trends and chart series."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models import AvailabilitySnapshot


@dataclass
class Trend:
    distributor: str
    first_qty: int | None
    last_qty: int | None
    qty_delta_pct: float | None
    first_min_price: float | None
    last_min_price: float | None
    price_delta_pct: float | None
    direction: str = "flat"  # up | down | flat


@dataclass
class History:
    series: list[dict] = field(default_factory=list)  # [{ts, total_stock, min_price}]
    trends: list[Trend] = field(default_factory=list)
    latest_total_stock: int | None = None
    latest_min_price: float | None = None
    price_delta_pct: float | None = None

    @property
    def price_trend_up(self) -> bool:
        return bool(self.price_delta_pct and self.price_delta_pct > 5.0)


def _min_price(snap: AvailabilitySnapshot) -> float | None:
    breaks = snap.price_breaks or []
    if breaks:
        return min((b.get("price", 0) for b in breaks if b.get("price")), default=None)
    return None


def _delta_pct(first, last) -> float | None:
    if first is None or last is None or first == 0:
        return None
    return round((last - first) / first * 100, 1)


def build_history(snapshots: list[AvailabilitySnapshot]) -> History:
    history = History()
    ordered = sorted(snapshots, key=lambda s: s.checked_at)

    by_dist: dict[str, list[AvailabilitySnapshot]] = {}
    for snap in ordered:
        by_dist.setdefault(snap.distributor, []).append(snap)

    for dist, snaps in by_dist.items():
        first, last = snaps[0], snaps[-1]
        fq, lq = first.available_qty, last.available_qty
        fp, lp = _min_price(first), _min_price(last)
        qty_delta = _delta_pct(fq, lq)
        price_delta = _delta_pct(fp, lp)
        direction = "up" if (price_delta or 0) > 5 else "down" if (price_delta or 0) < -5 else "flat"
        history.trends.append(
            Trend(
                distributor=dist,
                first_qty=fq,
                last_qty=lq,
                qty_delta_pct=qty_delta,
                first_min_price=fp,
                last_min_price=lp,
                price_delta_pct=price_delta,
                direction=direction,
            )
        )

    # Aggregate series across distributors per check timestamp.
    by_ts: dict[str, dict] = {}
    for snap in ordered:
        key = snap.checked_at.isoformat(timespec="minutes")
        bucket = by_ts.setdefault(key, {"ts": key, "total_stock": 0, "min_price": None})
        bucket["total_stock"] += snap.available_qty or 0
        price = _min_price(snap)
        if price is not None:
            bucket["min_price"] = price if bucket["min_price"] is None else min(bucket["min_price"], price)
    history.series = list(by_ts.values())

    if history.trends:
        history.latest_min_price = min(
            (t.last_min_price for t in history.trends if t.last_min_price is not None), default=None
        )
        history.latest_total_stock = sum(t.last_qty or 0 for t in history.trends)
        price_deltas = [t.price_delta_pct for t in history.trends if t.price_delta_pct is not None]
        if price_deltas:
            history.price_delta_pct = round(sum(price_deltas) / len(price_deltas), 1)
    return history
