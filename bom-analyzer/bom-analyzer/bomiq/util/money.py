"""
Money handling: currency normalisation, FX conversion and price-break maths.

All monetary arithmetic uses ``Decimal`` with explicit quantisation so that
totals reconcile to the cent no matter how many lines a BOM has.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable, Sequence

from .text import clean

CENT = Decimal("0.01")
MICRO = Decimal("0.000001")

CURRENCY_SYMBOLS = {
    "$": "USD", "US$": "USD", "USD": "USD",
    "€": "EUR", "EUR": "EUR",
    "£": "GBP", "GBP": "GBP",
    "¥": "JPY", "JPY": "JPY", "JP¥": "JPY",
    "₹": "INR", "INR": "INR", "RS": "INR", "RS.": "INR",
    "CNY": "CNY", "RMB": "CNY", "￥": "CNY",
    "CAD": "CAD", "C$": "CAD", "AUD": "AUD", "A$": "AUD",
    "CHF": "CHF", "SEK": "SEK", "SGD": "SGD", "S$": "SGD",
    "KRW": "KRW", "₩": "KRW", "HKD": "HKD", "TWD": "TWD",
    "MXN": "MXN", "BRL": "BRL", "PLN": "PLN", "NOK": "NOK", "DKK": "DKK",
    "ILS": "ILS", "TRY": "TRY", "ZAR": "ZAR", "NZD": "NZD", "THB": "THB",
    "MYR": "MYR", "PHP": "PHP", "VND": "VND", "IDR": "IDR", "CZK": "CZK",
    "HUF": "HUF", "RON": "RON",
}

# Static fallback rates (units of currency per 1 USD). These are only used when
# no live rate table has been supplied; every converted figure is flagged in the
# UI/report so the user knows an indicative rate was applied.
FALLBACK_RATES_PER_USD: dict[str, Decimal] = {
    "USD": Decimal("1"),
    "EUR": Decimal("0.92"),
    "GBP": Decimal("0.79"),
    "JPY": Decimal("152"),
    "INR": Decimal("83.4"),
    "CNY": Decimal("7.24"),
    "CAD": Decimal("1.36"),
    "AUD": Decimal("1.52"),
    "CHF": Decimal("0.89"),
    "SEK": Decimal("10.5"),
    "SGD": Decimal("1.34"),
    "KRW": Decimal("1340"),
    "HKD": Decimal("7.82"),
    "TWD": Decimal("31.8"),
    "MXN": Decimal("17.1"),
    "BRL": Decimal("5.05"),
    "PLN": Decimal("3.97"),
    "NOK": Decimal("10.6"),
    "DKK": Decimal("6.87"),
    "ILS": Decimal("3.71"),
    "TRY": Decimal("32.2"),
    "ZAR": Decimal("18.7"),
    "NZD": Decimal("1.64"),
    "THB": Decimal("35.8"),
    "MYR": Decimal("4.72"),
    "PHP": Decimal("56.3"),
    "VND": Decimal("24700"),
    "IDR": Decimal("15700"),
    "CZK": Decimal("23.2"),
    "HUF": Decimal("360"),
    "RON": Decimal("4.57"),
}


def normalize_currency(value: object, default: str = "USD") -> str:
    """Map a symbol or code onto a three-letter ISO currency code."""
    text = clean(value).upper().replace(" ", "")
    if not text:
        return default
    if text in CURRENCY_SYMBOLS:
        return CURRENCY_SYMBOLS[text]
    if len(text) == 3 and text.isalpha():
        return text
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol and symbol in text:
            return code
    return default


_MONEY_RE = re.compile(r"-?\d[\d,.\s']*")


def detect_decimal_comma(values: Iterable[object]) -> bool:
    """Decide whether a *column* of numbers uses the comma as its decimal mark.

    A single cell such as ``0,029`` is ambiguous. A column is not: if any cell
    has a comma followed by one, two, or four-or-more digits, and no cell mixes
    in a period as a decimal point, the column is European. This is what lets
    ``0,0032`` and ``0,029`` in the same column both read correctly.
    """
    decisive = 0
    period_decimal = 0
    for value in values:
        text = clean(value)
        if not text:
            continue
        match = _MONEY_RE.search(text)
        if not match:
            continue
        number = match.group(0).replace(" ", "").replace("'", "")
        if "," in number and "." in number:
            if number.rfind(".") > number.rfind(","):
                period_decimal += 1
            else:
                decisive += 1
            continue
        if "," in number and number.count(",") == 1:
            tail = number.split(",")[-1]
            if len(tail) != 3:
                decisive += 1
        elif "." in number and number.count(".") == 1:
            tail = number.split(".")[-1]
            if len(tail) != 3:
                period_decimal += 1
    return decisive > 0 and decisive >= period_decimal


def parse_money(value: object, decimal_comma: bool | None = None
                ) -> tuple[Decimal | None, str | None]:
    """Parse a price cell into ``(amount, currency_or_None)``.

    Handles ``$1,234.56``, ``1.234,56 EUR``, ``INR 12.50``, ``1 234,50 €``.

    ``decimal_comma`` forces the interpretation of a lone comma, which is the
    only genuinely ambiguous case (``1,000``). Pass the result of
    :func:`detect_decimal_comma` for the whole column to get it right.

    >>> parse_money("$1,234.56")
    (Decimal('1234.56'), 'USD')
    >>> parse_money("0,029", decimal_comma=True)
    (Decimal('0.029'), None)
    """
    text = clean(value)
    if not text:
        return None, None
    currency = None
    for symbol in sorted(CURRENCY_SYMBOLS, key=len, reverse=True):
        if symbol and symbol in text.upper():
            currency = CURRENCY_SYMBOLS[symbol]
            break
    match = _MONEY_RE.search(text)
    if not match:
        return None, currency
    number = match.group(0).strip().replace(" ", "").replace("'", "")
    # Decide which separator is the decimal point. With both present, the
    # rightmost one is the decimal separator.
    if "," in number and "." in number:
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        tail = number.split(",")[-1]
        if number.count(",") > 1:
            # 1,234,567 -- only thousands separators make sense.
            number = number.replace(",", "")
        elif len(tail) == 3:
            # Genuinely ambiguous: "1,000" is a thousand in the US and one in
            # Germany. Use the column-level hint when the caller has one;
            # otherwise read it as a thousands separator, which is the more
            # common case in the files this tool sees.
            number = number.replace(",", "." if decimal_comma else "")
        else:
            # 1 or 2 digits (0,12) or 4+ digits (0,0032) -- decimal comma.
            number = number.replace(",", ".")
    try:
        return Decimal(number), currency
    except InvalidOperation:
        return None, currency


def to_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int,)):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    amount, _ = parse_money(value)
    return amount


class FxTable:
    """Currency conversion table.

    ``rates`` maps an ISO code to units-per-USD. Missing currencies fall back to
    the static indicative table; anything still unknown converts 1:1 and is
    reported through :attr:`unknown` so the caller can warn the user.
    """

    def __init__(self, rates: dict[str, Decimal] | None = None,
                 base: str = "USD", source: str = "indicative") -> None:
        self.base = base
        self.source = source
        self.rates: dict[str, Decimal] = dict(FALLBACK_RATES_PER_USD)
        if rates:
            for code, rate in rates.items():
                try:
                    self.rates[normalize_currency(code)] = Decimal(str(rate))
                except (InvalidOperation, TypeError):
                    continue
        self.unknown: set[str] = set()

    def convert(self, amount: Decimal | None, source_currency: str,
                target_currency: str) -> Decimal | None:
        if amount is None:
            return None
        src = normalize_currency(source_currency)
        dst = normalize_currency(target_currency)
        if src == dst:
            return amount
        rate_src = self.rates.get(src)
        rate_dst = self.rates.get(dst)
        if rate_src is None or rate_dst is None:
            self.unknown.add(src if rate_src is None else dst)
            return amount
        return (amount / rate_src) * rate_dst

    def is_indicative(self) -> bool:
        return self.source == "indicative"


def quantize(amount: Decimal | None, places: Decimal = CENT) -> Decimal | None:
    if amount is None:
        return None
    return amount.quantize(places, rounding=ROUND_HALF_UP)


def format_money(amount: Decimal | float | None, currency: str = "USD",
                 places: int | None = None) -> str:
    """Render an amount with a sensible number of decimals."""
    value = to_decimal(amount)
    if value is None:
        return ""
    if places is None:
        magnitude = abs(value)
        if magnitude == 0:
            places = 2
        elif magnitude < Decimal("0.01"):
            places = 5
        elif magnitude < Decimal("1"):
            places = 4
        else:
            places = 2
    quantum = Decimal(1).scaleb(-places)
    text = f"{value.quantize(quantum, rounding=ROUND_HALF_UP):,}"
    return f"{text} {normalize_currency(currency)}"


# --------------------------------------------------------------------------- #
# Price breaks
# --------------------------------------------------------------------------- #

def price_at_quantity(breaks: Sequence[tuple[int, Decimal]], quantity: float
                      ) -> tuple[Decimal | None, int | None]:
    """Unit price applicable at ``quantity`` from a price-break ladder.

    ``breaks`` is a sequence of ``(min_qty, unit_price)`` pairs in any order.
    Returns ``(unit_price, break_qty)``; when the quantity is below the lowest
    break the lowest break is returned (distributors will not sell cheaper) and
    the caller can see ``break_qty > quantity``.

    >>> price_at_quantity([(1, Decimal("1.00")), (10, Decimal("0.80"))], 12)
    (Decimal('0.80'), 10)
    """
    ladder = sorted(
        ((int(q), Decimal(str(p))) for q, p in breaks if q is not None and p is not None),
        key=lambda item: item[0],
    )
    if not ladder:
        return None, None
    chosen = ladder[0]
    for min_qty, unit_price in ladder:
        if quantity >= min_qty:
            chosen = (min_qty, unit_price)
        else:
            break
    return chosen[1], chosen[0]


def order_quantity(required: float, moq: int | None, spq: int | None) -> int:
    """Purchasable quantity honouring minimum-order and standard-pack sizes.

    >>> order_quantity(37, moq=10, spq=25)
    50
    """
    need = max(0, int(-(-required // 1)))  # ceil
    if need == 0:
        return 0
    if moq:
        need = max(need, int(moq))
    if spq and spq > 1:
        packs = -(-need // int(spq))
        need = packs * int(spq)
    return need


def extended_price(unit_price: Decimal | None, quantity: float) -> Decimal | None:
    if unit_price is None:
        return None
    return Decimal(str(unit_price)) * Decimal(str(quantity))


def cheapest_break(breaks: Sequence[tuple[int, Decimal]]) -> Decimal | None:
    prices = [Decimal(str(p)) for _, p in breaks if p is not None]
    return min(prices) if prices else None


def sum_money(values: Iterable[Decimal | None]) -> Decimal:
    total = Decimal("0")
    for value in values:
        if value is not None:
            total += Decimal(str(value))
    return total
