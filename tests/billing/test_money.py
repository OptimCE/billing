from decimal import Decimal

from utils.money import line_amount, round_money, vat_amount


def test_round_money_half_up():
    assert round_money(Decimal("2.005")) == Decimal("2.01")
    assert round_money(Decimal("2.004")) == Decimal("2.00")
    assert round_money(Decimal("2.015")) == Decimal("2.02")
    # Half away from zero for negatives too (credit notes).
    assert round_money(Decimal("-2.005")) == Decimal("-2.01")


def test_line_amount_keeps_full_kwh_precision_until_money_step():
    # 100.123456 kWh x 0.150000 €/kWh = 15.0185184 → 15.02
    assert line_amount(Decimal("100.123456"), Decimal("0.15")) == Decimal("15.02")


def test_vat_amount_rounds_at_total():
    assert vat_amount(Decimal("100.00"), Decimal("0.21")) == Decimal("21.00")
    # 33.33 x 0.21 = 6.9993 → 7.00
    assert vat_amount(Decimal("33.33"), Decimal("0.21")) == Decimal("7.00")
