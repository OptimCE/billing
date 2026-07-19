from decimal import Decimal

import pytest

from regime.registry import (
    RegimeConfigError,
    assert_parity,
    assert_regime_parity,
    get_registry,
)
from shared.const import InvoiceType, Measure


def test_registry_has_cwape():
    regime = get_registry().get_for("BE-WAL-CWAPE")
    assert regime.code == "BE-WAL-CWAPE"


def test_cwape_vat_due_producer():
    regime = get_registry().get_for("BE-WAL-CWAPE")
    assert regime.vat_rate(member_type=1, direction=1, social_rate=False) == Decimal("0.21")
    # No legal "tarif social" on shared energy → social_rate does not change VAT.
    assert regime.vat_rate(member_type=1, direction=1, social_rate=True) == Decimal("0.21")
    assert regime.due_days() == 30
    assert regime.producer_mode() == "PRODUCER_STATEMENT"


def test_cwape_billable_measures():
    regime = get_registry().get_for("BE-WAL-CWAPE")
    measures = regime.billable_measures()
    assert measures["consumer"] == [Measure.SHARED]
    assert measures["producer"] == [Measure.INJ_SHARED]


def test_cwape_number_format_and_series():
    regime = get_registry().get_for("BE-WAL-CWAPE")
    assert regime.format_number(doc_type=InvoiceType.INVOICE, year=2026, seq=1) == "F-2026-00001"
    assert (
        regime.format_number(doc_type=InvoiceType.CREDIT_NOTE, year=2026, seq=42) == "NC-2026-00042"
    )
    assert regime.series_prefix(InvoiceType.PRODUCER_STATEMENT) == "DP"


def test_cwape_legal_mentions():
    regime = get_registry().get_for("BE-WAL-CWAPE")
    assert regime.legal_mentions(locale="fr-BE")
    assert regime.legal_mentions(locale="fr")  # falls back to fr-*
    assert regime.legal_mentions(locale="xx-YY") == []


def test_get_for_unknown_regulator_raises():
    with pytest.raises(RegimeConfigError):
        get_registry().get_for("BE-BRU-BRUGEL")


def test_parity_passes_for_real_config():
    # Vendored tests/fixtures/regulators.json (copy of the shared reference file,
    # CWaPE active) + billing_regimes.json; wired via REGULATORS_CONFIG_PATH in conftest.
    assert_regime_parity()


def test_parity_detects_active_regulator_without_strategy():
    regulators = [
        {"code": "BE-WAL-CWAPE", "active": True},
        {"code": "BE-BRU-BRUGEL", "active": True},  # active but no strategy
    ]
    with pytest.raises(RegimeConfigError):
        assert_parity(regulators, get_registry())


def test_parity_detects_strategy_without_active_regulator():
    regulators = [{"code": "BE-WAL-CWAPE", "active": False}]  # strategy exists but inactive
    with pytest.raises(RegimeConfigError):
        assert_parity(regulators, get_registry())
