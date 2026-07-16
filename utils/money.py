"""Money arithmetic. kWh volumes and prices stay at full Decimal precision; the
round to 2 decimals (half-up) happens ONLY at the money step — line amount, VAT,
and totals — never on intermediate kWh values.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_CENTS = Decimal("0.01")


def round_money(value: Decimal) -> Decimal:
    """Quantize to 2 decimals, rounding half away from zero (half-up)."""
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


def line_amount(quantity_kwh: Decimal, unit_price: Decimal) -> Decimal:
    """A line's money amount: kWh (full precision) x unit price, rounded to cents."""
    return round_money(quantity_kwh * unit_price)


def vat_amount(subtotal: Decimal, vat_rate: Decimal) -> Decimal:
    """VAT on a net subtotal, rounded to cents."""
    return round_money(subtotal * vat_rate)
