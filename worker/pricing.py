"""Pure pricing: turn frozen snapshots + resolved unit prices into DRAFT invoices.

No I/O — prices are resolved by the caller and passed in, so this is unit-testable
in isolation. Money is rounded half-up to cents only at the line/VAT/total steps;
kWh volumes keep full precision until then.
"""

from __future__ import annotations

from decimal import Decimal

from regime.base import BillingRegime
from shared.const import BillingDirection, InvoiceStatus, InvoiceType, Measure
from shared.models.local_models import InvoiceLineModel, InvoiceModel, SettlementSnapshotModel
from utils.money import line_amount, round_money, vat_amount


def _quantity(snapshot: SettlementSnapshotModel) -> Decimal:
    if snapshot.direction == BillingDirection.CONSUMER:
        return snapshot.shared_kwh
    return snapshot.inj_shared_kwh


def _measure(direction: int) -> Measure:
    return Measure.SHARED if direction == BillingDirection.CONSUMER else Measure.INJ_SHARED


def _doc_type(direction: int) -> InvoiceType:
    return (
        InvoiceType.INVOICE
        if direction == BillingDirection.CONSUMER
        else InvoiceType.PRODUCER_STATEMENT
    )


def _description(direction: int, ean: str) -> str:
    if direction == BillingDirection.CONSUMER:
        return f"Énergie partagée consommée — EAN {ean}"
    return f"Rémunération de l'injection partagée — EAN {ean}"


def build_invoices(
    *,
    id_community: int,
    id_billing_run: int,
    snapshots: list[SettlementSnapshotModel],
    prices: dict[int, tuple[Decimal, str]],
    regime: BillingRegime,
) -> list[InvoiceModel]:
    """Group snapshots by (member, direction) into one DRAFT invoice each.

    ``prices`` maps a snapshot id to (unit_price, currency). Snapshots without a
    member or with a zero volume are skipped (they carry no billable line).
    """
    grouped: dict[tuple[int, int], list[SettlementSnapshotModel]] = {}
    for snapshot in snapshots:
        if snapshot.id_member is None:
            continue
        if _quantity(snapshot) <= 0:
            continue
        grouped.setdefault((snapshot.id_member, snapshot.direction), []).append(snapshot)

    invoices: list[InvoiceModel] = []
    for (id_member, direction), rows in grouped.items():
        doc_type = _doc_type(direction)
        vat_rate = regime.vat_rate(member_type=0, direction=direction, social_rate=False)

        lines: list[InvoiceLineModel] = []
        currency = "EUR"
        for snapshot in rows:
            unit_price, currency = prices[snapshot.id]
            quantity = _quantity(snapshot)
            lines.append(
                InvoiceLineModel(
                    id_community=id_community,
                    ean=snapshot.ean,
                    direction=direction,
                    measure=_measure(direction),
                    quantity_kwh=quantity,
                    unit_price=unit_price,
                    amount=line_amount(quantity, unit_price),
                    description=_description(direction, snapshot.ean),
                )
            )

        subtotal = round_money(sum((line.amount for line in lines), Decimal(0)))
        vat = vat_amount(subtotal, vat_rate)
        invoices.append(
            InvoiceModel(
                id_community=id_community,
                id_billing_run=id_billing_run,
                id_member=id_member,
                doc_type=doc_type,
                status=InvoiceStatus.DRAFT,
                legal_entity_key=f"community:{id_community}:{regime.series_prefix(doc_type)}",
                currency=currency,
                subtotal=subtotal,
                vat_rate=vat_rate,
                vat_amount=vat,
                total=subtotal + vat,
                lines=lines,
            )
        )
    return invoices
