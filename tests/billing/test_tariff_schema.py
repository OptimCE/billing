"""`TariffIn` validation — the one place this service mints an EAN.

Pure schema tests: no database, no broker. `TariffModel` is constructed
directly elsewhere (see `test_tariff_resolution.py`), which deliberately
bypasses this validator — the guard is on the API boundary, not the model.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from api.billing.schemas import TariffIn
from shared.const import TariffKind, TariffScope

_JAN = datetime.date(2026, 1, 1)


def _payload(**overrides):
    base = {
        "kind": TariffKind.CONSUMER_SELLING,
        "scope": TariffScope.EAN,
        "scope_ean": "541448200000000001",
        "price_per_kwh": Decimal("0.30"),
        "valid_from": _JAN,
    }
    base.update(overrides)
    return base


def test_accepts_an_18_digit_ean():
    assert TariffIn(**_payload()).scope_ean == "541448200000000001"


def test_accepts_an_18_digit_ean_with_surrounding_whitespace():
    # Matched after strip, like every other EAN entry point in the platform.
    tariff = TariffIn(**_payload(scope_ean="  541448200000000001  "))
    assert tariff.scope_ean.strip() == "541448200000000001"


@pytest.mark.parametrize(
    "ean",
    [
        "1234567890123",  # the old, wrong 13-digit rule
        "54144820000000001",  # 17
        "5414482000000000011",  # 19
        "54144820000000000A",
        "EAN-1",  # the shape older fixtures used
    ],
)
def test_rejects_anything_that_is_not_18_digits(ean):
    # A typo here does not fail loudly: it produces a tariff that resolves for
    # nobody while pricing falls through to SEGMENT then GLOBAL.
    with pytest.raises(ValueError, match="18-digit EAN"):
        TariffIn(**_payload(scope_ean=ean))


def test_still_requires_an_ean_for_ean_scope():
    with pytest.raises(ValueError, match="scope_ean is required"):
        TariffIn(**_payload(scope_ean=None))


@pytest.mark.parametrize("scope", [TariffScope.GLOBAL, TariffScope.SEGMENT])
def test_other_scopes_are_untouched_by_the_ean_rule(scope):
    extra = {"scope_segment": 2} if scope == TariffScope.SEGMENT else {}
    tariff = TariffIn(**_payload(scope=scope, scope_ean=None, **extra))
    assert tariff.scope_ean is None
