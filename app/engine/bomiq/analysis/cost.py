"""
Costing and sourcing optimisation.

For each line the engine answers four questions:

1. **What does it cost to buy the quantity this build needs?** Honouring the
   price-break ladder, minimum order quantity and standard pack size -- a part
   you need 37 of but which ships in reels of 4000 costs a reel, and the
   report says so rather than quietly costing 37 pieces.
2. **Which distributor is cheapest for that quantity?** Compared in one
   currency, with the overbuy cost included so a "cheap" unit price behind a
   huge MOQ does not win.
3. **What if no single distributor has enough?** A greedy split across
   distributors is computed so the buyer sees a workable basket.
4. **How does unit cost move with build quantity?** A price curve at 1 / 10 /
   100 / 1k / 10k so an engineer can see where the breaks land.

All arithmetic uses ``Decimal``; totals reconcile exactly.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING
from typing import Sequence

from ..config import Settings
from ..core.models import Cost, Offer, PartData, SourcingOption
from ..util.money import (
    FxTable, extended_price, order_quantity, price_at_quantity, quantize,
)
from ..util.text import clean

CURVE_QUANTITIES = (1, 10, 100, 1_000, 10_000)


def required_quantity(per_assembly_qty: float, build_quantity: int) -> int:
    """Pieces needed for the whole build, rounded up exactly.

    Computed in ``Decimal`` because the obvious float version over-counts: a
    0.55/board part over 100 boards is mathematically 55, but
    ``0.55 * 100`` is 55.00000000000001 in binary floating point and ceils
    to 56.

    >>> required_quantity(0.55, 100)
    55
    >>> required_quantity(0.3, 10)
    3
    >>> required_quantity(1.5, 3)
    5
    """
    if per_assembly_qty <= 0 or build_quantity <= 0:
        return 0
    exact = Decimal(str(per_assembly_qty)) * Decimal(int(max(1, build_quantity)))
    return int(exact.to_integral_value(rounding=ROUND_CEILING))


# --------------------------------------------------------------------------- #
# Single offer costing
# --------------------------------------------------------------------------- #

def price_offer(offer: Offer, required_qty: int, target_currency: str,
                fx: FxTable) -> SourcingOption | None:
    """Cost ``required_qty`` from one offer, honouring MOQ and pack size."""
    if required_qty <= 0:
        return None
    ladder = offer.break_tuples
    if not ladder:
        return None

    order_qty = order_quantity(required_qty, offer.moq,
                               offer.spq or offer.order_multiple)
    unit_native, break_qty = price_at_quantity(ladder, order_qty)
    if unit_native is None:
        return None

    unit = fx.convert(unit_native, offer.currency, target_currency)
    extended = extended_price(unit, order_qty)
    overbuy = max(0, order_qty - required_qty)

    notes: list[str] = []
    if offer.moq and offer.moq > required_qty:
        notes.append(f"Minimum order is {offer.moq:,}")
    if (offer.spq or 0) > 1 and order_qty % (offer.spq or 1) == 0 and \
            order_qty > required_qty:
        notes.append(f"Sold in packs of {offer.spq:,}")
    if break_qty and break_qty > order_qty:
        notes.append(f"Priced at the {break_qty:,}-piece break")
    if offer.currency != target_currency:
        notes.append(
            f"Converted from {offer.currency}"
            f"{' at an indicative rate' if fx.is_indicative() else ''}")
    if not offer.authorized:
        notes.append("Not an authorised distributor")
    notes.extend(offer.warnings)

    covers = (offer.stock or 0) >= order_qty
    if not covers and offer.stock is not None:
        notes.append(f"Only {offer.stock:,} in stock")

    return SourcingOption(
        provider=offer.provider,
        distributor=offer.distributor,
        sku=offer.sku,
        order_qty=order_qty,
        unit_price=quantize(unit, Decimal("0.000001")),
        break_qty=break_qty,
        extended=quantize(extended),
        currency=target_currency,
        native_unit_price=unit_native,
        native_currency=offer.currency,
        stock=offer.stock,
        lead_time_days=offer.lead_time_days,
        moq=offer.moq,
        spq=offer.spq or offer.order_multiple,
        covers_demand=covers,
        overbuy_qty=overbuy,
        overbuy_cost=quantize(extended_price(unit, overbuy)),
        url=offer.url,
        notes=notes,
    )


def _sort_key(option: SourcingOption) -> tuple[int, Decimal, int]:
    """Cheapest *total* wins; stocked offers beat back-ordered ones."""
    return (
        0 if option.covers_demand else 1,
        option.extended if option.extended is not None else Decimal("9" * 12),
        option.lead_time_days if option.lead_time_days is not None else 9999,
    )


# --------------------------------------------------------------------------- #
# Line costing
# --------------------------------------------------------------------------- #

def cost_line(part: PartData | None, per_assembly_qty: float,
              settings: Settings, fx: FxTable) -> Cost:
    """Full costing for one line at the configured build quantity."""
    target_currency = clean(settings.currency).upper() or "USD"
    required = required_quantity(per_assembly_qty, settings.build_quantity)
    cost = Cost(required_qty=required, currency=target_currency)

    if per_assembly_qty <= 0:
        cost.notes.append("Not populated — excluded from the cost total.")
        return cost
    if part is None:
        cost.notes.append("No catalogue data, so this line is not costed.")
        return cost

    offers = part.offers
    if settings.prefer_authorized_only:
        authorized = [offer for offer in offers if offer.authorized]
        if authorized:
            offers = authorized
    if not settings.include_non_stocking_offers:
        stocking = [offer for offer in offers if offer.in_stock]
        if stocking:
            offers = stocking

    options: list[SourcingOption] = []
    for offer in offers:
        option = price_offer(offer, required, target_currency, fx)
        if option is not None:
            options.append(option)

    if not options:
        # Fall back to the aggregate median price so the BOM still totals.
        if part.median_price_1k:
            unit = fx.convert(part.median_price_1k,
                              part.median_price_1k_currency or "USD",
                              target_currency)
            cost.unit_price = quantize(unit, Decimal("0.000001"))
            cost.extended = quantize(extended_price(unit, required))
            cost.estimated = True
            cost.notes.append(
                "No purchasable offer was found; costed at the aggregated "
                "median 1k price, which is indicative only.")
        else:
            cost.notes.append("No price was available for this line.")
        return cost

    options.sort(key=_sort_key)
    cost.options = options
    cost.best = options[0]
    cost.unit_price = cost.best.unit_price
    cost.extended = cost.best.extended
    cost.indicative_fx = fx.is_indicative() and any(
        option.native_currency != target_currency for option in options)

    # Savings and spread compare *unit prices at the required quantity*, not
    # extended totals. Extended totals are not comparable across offers whose
    # minimum order differs -- a 4000-piece reel always "costs more" than 500
    # of cut tape, and counting that gap as a saving would invent money that
    # was never on the table. Comparing the price per piece answers the
    # question a buyer actually asks: how much does choosing the cheaper
    # distributor save on the quantity I need?
    # Only offers that can actually supply the quantity count towards the
    # saving. Measuring against an offer nobody could place would report money
    # that was never on the table.
    purchasable = [option for option in options
                   if option.unit_price is not None and option.covers_demand]
    units = [option.unit_price for option in (purchasable or options)
             if option.unit_price is not None]
    if len(units) > 1:
        cheapest, dearest = min(units), max(units)
        cost.savings_vs_worst = quantize(
            (dearest - cheapest) * Decimal(max(1, required)))
        if cheapest > 0:
            cost.price_spread_pct = float(
                ((dearest - cheapest) / cheapest) * 100)

    if not cost.best.covers_demand:
        offers_by_key = {(offer.distributor, offer.sku): offer
                         for offer in offers}
        split = split_across_distributors(
            options, required, offers_by_key=offers_by_key,
            target_currency=target_currency, fx=fx)
        if split:
            cost.notes.append(
                "No single distributor can cover the quantity; a split order "
                "is suggested below.")
            cost.options = split + [o for o in options if o not in split]
            covered = sum(option.order_qty for option in split)
            total = sum((option.extended or Decimal("0")) for option in split)
            if covered >= required:
                cost.extended = quantize(total)
                cost.unit_price = quantize(total / Decimal(required),
                                           Decimal("0.000001"))
                cost.best = split[0]

    cost.price_curve = price_curve(part, per_assembly_qty, target_currency, fx)
    return cost


def split_across_distributors(options: Sequence[SourcingOption],
                              required: int, offers_by_key: dict | None = None,
                              target_currency: str = "USD",
                              fx: FxTable | None = None
                              ) -> list[SourcingOption]:
    """Greedy multi-distributor basket when one source is not enough.

    Buys from the cheapest stocked source first, takes what it has, then moves
    on. Returns ``[]`` when a split cannot cover the requirement either.

    Each leg is **re-priced at its own quantity**. Reusing the unit price that
    was computed for the full requirement would quote a 400-piece leg at the
    500-piece break and understate the basket, so the price ladder is consulted
    again for every leg. A source whose minimum order exceeds what it actually
    has in stock is skipped rather than quoted below its MOQ.
    """
    remaining = required
    basket: list[SourcingOption] = []
    offers_by_key = offers_by_key or {}
    for option in sorted(options, key=lambda o: (
            o.unit_price if o.unit_price is not None else Decimal("9" * 12))):
        if remaining <= 0:
            break
        available = option.stock if option.stock is not None else remaining
        if available <= 0:
            continue
        take = order_quantity(min(remaining, available), option.moq, option.spq)
        if take > available:
            # Its minimum order or pack size is bigger than its stock, so this
            # source cannot contribute a partial leg.
            continue

        # Re-price this leg at its own quantity.
        unit = option.unit_price or Decimal("0")
        break_qty = option.break_qty
        offer = offers_by_key.get((option.distributor, option.sku))
        if offer is not None and offer.break_tuples:
            native, break_qty = price_at_quantity(offer.break_tuples, take)
            if native is not None:
                converted = (fx.convert(native, offer.currency, target_currency)
                             if fx is not None else native)
                if converted is not None:
                    unit = quantize(converted, Decimal("0.000001")) or unit

        basket.append(SourcingOption(
            provider=option.provider, distributor=option.distributor,
            sku=option.sku, order_qty=take, unit_price=unit,
            break_qty=break_qty,
            extended=quantize(unit * Decimal(take)),
            currency=option.currency,
            native_unit_price=option.native_unit_price,
            native_currency=option.native_currency,
            stock=option.stock, lead_time_days=option.lead_time_days,
            moq=option.moq, spq=option.spq, covers_demand=True,
            overbuy_qty=0, url=option.url,
            notes=[f"Part of a split order: {take:,} of {required:,} pieces"],
        ))
        remaining -= take
    if remaining > 0 or len(basket) < 2:
        return []
    return basket


def price_curve(part: PartData, per_assembly_qty: float, currency: str,
                fx: FxTable) -> list[dict[str, object]]:
    """Unit cost at a range of build quantities, for the detail panel chart."""
    curve: list[dict[str, object]] = []
    for build_qty in CURVE_QUANTITIES:
        required = required_quantity(per_assembly_qty, build_qty)
        if required <= 0:
            continue
        best_unit: Decimal | None = None
        best_distributor = ""
        for offer in part.offers:
            ladder = offer.break_tuples
            if not ladder:
                continue
            order_qty = order_quantity(required, offer.moq,
                                       offer.spq or offer.order_multiple)
            unit_native, _ = price_at_quantity(ladder, order_qty)
            if unit_native is None:
                continue
            unit = fx.convert(unit_native, offer.currency, currency)
            if unit is None:
                continue
            # Charge the overbuy across the pieces actually needed, so the
            # curve reflects real cost per board.
            effective = (unit * Decimal(order_qty)) / Decimal(required)
            if best_unit is None or effective < best_unit:
                best_unit = effective
                best_distributor = offer.distributor
        if best_unit is not None:
            curve.append({
                "build_qty": build_qty,
                "line_qty": required,
                "unit_price": str(quantize(best_unit, Decimal("0.000001"))),
                "extended": str(quantize(best_unit * Decimal(required))),
                "distributor": best_distributor,
                "currency": currency,
            })
    return curve


# --------------------------------------------------------------------------- #
# BOM roll-up
# --------------------------------------------------------------------------- #

def roll_up(costs: Sequence[Cost], build_quantity: int, currency: str
            ) -> dict[str, object]:
    """Aggregate line costs into BOM totals."""
    total = Decimal("0")
    savings = Decimal("0")
    costed = 0
    uncosted = 0
    for cost in costs:
        if cost.required_qty <= 0:
            continue
        if cost.extended is None:
            uncosted += 1
            continue
        total += cost.extended
        costed += 1
        if cost.savings_vs_worst:
            savings += cost.savings_vs_worst
    lines_needing_cost = costed + uncosted
    return {
        "total_cost": quantize(total),
        "cost_per_unit": quantize(total / Decimal(max(1, build_quantity))),
        "costed_lines": costed,
        "uncosted_lines": uncosted,
        "coverage_pct": round(100.0 * costed / lines_needing_cost, 1)
        if lines_needing_cost else 0.0,
        "potential_savings": quantize(savings),
        "currency": currency,
    }


def compare_to_input_prices(results: Sequence[object], currency: str,
                            fx: FxTable) -> dict[str, object]:
    """Compare live pricing against the prices already in the user's BOM.

    Returns the aggregate delta plus the biggest movers, which is usually the
    first thing a buyer wants to see when re-quoting an existing BOM.
    """
    total_old = Decimal("0")
    total_new = Decimal("0")
    movers: list[dict[str, object]] = []
    compared = 0

    for result in results:
        line = getattr(result, "line", None)
        cost = getattr(result, "cost", None)
        if line is None or cost is None:
            continue
        if line.unit_price_in is None or cost.unit_price is None:
            continue
        if line.effective_quantity <= 0:
            continue
        old_unit = fx.convert(line.unit_price_in,
                              line.currency_in or currency, currency)
        if old_unit is None or old_unit <= 0:
            continue
        new_unit = cost.unit_price
        quantity = Decimal(str(cost.required_qty or line.effective_quantity))
        total_old += old_unit * quantity
        total_new += new_unit * quantity
        compared += 1
        delta_pct = float(((new_unit - old_unit) / old_unit) * 100)
        if abs(delta_pct) >= 10:
            movers.append({
                "line_no": line.line_no,
                "mpn": line.mpn,
                "old_unit": str(quantize(old_unit, Decimal("0.000001"))),
                "new_unit": str(quantize(new_unit, Decimal("0.000001"))),
                "delta_pct": round(delta_pct, 1),
                "extended_delta": str(
                    quantize((new_unit - old_unit) * quantity)),
            })

    movers.sort(key=lambda item: -abs(float(item["delta_pct"])))
    delta = total_new - total_old
    return {
        "compared_lines": compared,
        "old_total": str(quantize(total_old)),
        "new_total": str(quantize(total_new)),
        "delta": str(quantize(delta)),
        "delta_pct": round(float((delta / total_old) * 100), 1)
        if total_old > 0 else None,
        "biggest_movers": movers[:15],
        "currency": currency,
    }
