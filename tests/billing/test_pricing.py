"""Pure unit tests for invoice building (no DB)."""

from __future__ import annotations

import datetime
from decimal import Decimal

from shared.const import BillingDirection, InvoiceStatus, InvoiceType, Measure
from shared.models.local_models import SettlementSnapshotModel
from worker.pricing import build_invoices


class FakeRegime:
    code = "TEST"

    def vat_rate(self, *, member_type: int, direction: int, social_rate: bool) -> Decimal:
        return Decimal("0.21")

    def series_prefix(self, doc_type: int) -> str:
        return {1: "F", 2: "NC", 3: "DP"}[int(doc_type)]


def _snap(
    snap_id: int,
    ean: str,
    direction: int,
    member: int | None,
    *,
    shared: Decimal = Decimal(0),
    inj: Decimal = Decimal(0),
    client_type: int = 1,
    owned_from: datetime.date | None = None,
    owned_to: datetime.date | None = None,
) -> SettlementSnapshotModel:
    snapshot = SettlementSnapshotModel(
        id_community=1,
        id_billing_run=1,
        ean=ean,
        direction=direction,
        client_type=client_type,
        shared_kwh=shared,
        inj_shared_kwh=inj,
        row_count=1,
        distinct_ts_count=1,
        id_member=member,
        owned_from=owned_from,
        owned_to=owned_to,
    )
    snapshot.id = snap_id  # PK is normally DB-assigned; set for the prices map
    return snapshot


def test_build_invoices_groups_by_member_and_direction():
    snaps = [
        _snap(1, "EAN-C", BillingDirection.CONSUMER, 10, shared=Decimal("30")),
        _snap(2, "EAN-P", BillingDirection.PRODUCER, 20, inj=Decimal("15")),
    ]
    prices = {1: (Decimal("0.15"), "EUR"), 2: (Decimal("0.08"), "EUR")}
    invoices = build_invoices(
        id_community=1, id_billing_run=1, snapshots=snaps, prices=prices, regime=FakeRegime()
    )
    by_type = {inv.doc_type: inv for inv in invoices}

    consumer = by_type[InvoiceType.INVOICE]
    assert consumer.id_member == 10
    assert consumer.status == InvoiceStatus.DRAFT
    assert consumer.subtotal == Decimal("4.50")  # 30 x 0.15
    assert consumer.vat_amount == Decimal("0.95")  # 0.945 → 0.95 half-up
    assert consumer.total == Decimal("5.45")
    assert consumer.legal_entity_key == "community:1:F"
    assert len(consumer.lines) == 1
    assert consumer.lines[0].measure == Measure.SHARED

    producer = by_type[InvoiceType.PRODUCER_STATEMENT]
    assert producer.id_member == 20
    assert producer.subtotal == Decimal("1.20")  # 15 x 0.08
    assert producer.legal_entity_key == "community:1:DP"
    assert producer.lines[0].measure == Measure.INJ_SHARED


def test_build_invoices_skips_zero_volume_and_memberless():
    snaps = [
        _snap(1, "EAN-Z", BillingDirection.CONSUMER, 10, shared=Decimal("0")),
        _snap(2, "EAN-N", BillingDirection.CONSUMER, None, shared=Decimal("5")),
    ]
    invoices = build_invoices(
        id_community=1,
        id_billing_run=1,
        snapshots=snaps,
        prices={2: (Decimal("0.15"), "EUR")},
        regime=FakeRegime(),
    )
    assert invoices == []


def test_line_description_carries_ownership_window_when_set():
    snaps = [
        _snap(
            1,
            "EAN-W",
            BillingDirection.CONSUMER,
            10,
            shared=Decimal("30"),
            owned_from=datetime.date(2026, 6, 1),
            owned_to=datetime.date(2026, 6, 15),
        ),
    ]
    invoices = build_invoices(
        id_community=1,
        id_billing_run=1,
        snapshots=snaps,
        prices={1: (Decimal("0.15"), "EUR")},
        regime=FakeRegime(),
    )
    assert invoices[0].lines[0].description.endswith("— du 01/06/2026 au 15/06/2026")


def test_line_description_has_no_window_for_full_period_snapshot():
    snaps = [_snap(1, "EAN-F", BillingDirection.CONSUMER, 10, shared=Decimal("30"))]
    invoices = build_invoices(
        id_community=1,
        id_billing_run=1,
        snapshots=snaps,
        prices={1: (Decimal("0.15"), "EUR")},
        regime=FakeRegime(),
    )
    assert "du " not in invoices[0].lines[0].description
    assert invoices[0].lines[0].description == "Énergie partagée consommée — EAN EAN-F"


def test_build_invoices_one_invoice_many_lines_for_same_member():
    snaps = [
        _snap(1, "EAN-A", BillingDirection.CONSUMER, 10, shared=Decimal("10")),
        _snap(2, "EAN-B", BillingDirection.CONSUMER, 10, shared=Decimal("20")),
    ]
    prices = {1: (Decimal("0.10"), "EUR"), 2: (Decimal("0.10"), "EUR")}
    invoices = build_invoices(
        id_community=1, id_billing_run=1, snapshots=snaps, prices=prices, regime=FakeRegime()
    )
    assert len(invoices) == 1
    assert len(invoices[0].lines) == 2
    assert invoices[0].subtotal == Decimal("3.00")  # 1.00 + 2.00
