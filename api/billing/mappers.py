"""ORM ↔ DTO mapping for the billing API.

Relationship collections are passed in explicitly (loaded via selectinload in the
repository) rather than read off the model, to avoid async lazy-load surprises.
"""

from __future__ import annotations

from api.billing.schemas import (
    BillingRunOut,
    InvoiceLineOut,
    InvoiceOut,
    PaymentOut,
    TariffOut,
)
from ports.crm_core import CommunityIdentity, ParticipantContact
from regime.base import BillingRegime
from shared.models.local_models import (
    BillingRunModel,
    InvoiceLineModel,
    InvoiceModel,
    PaymentModel,
    TariffModel,
)


def tariff_to_out(tariff: TariffModel) -> TariffOut:
    return TariffOut(
        id=tariff.id,
        id_sharing_operation=tariff.id_sharing_operation,
        kind=tariff.kind,
        scope=tariff.scope,
        scope_segment=tariff.scope_segment,
        scope_ean=tariff.scope_ean,
        price_per_kwh=tariff.price_per_kwh,
        currency=tariff.currency,
        valid_from=tariff.valid_from,
        valid_to=tariff.valid_to,
        label=tariff.label,
    )


def run_to_out(run: BillingRunModel, *, invoice_count: int | None = None) -> BillingRunOut:
    return BillingRunOut(
        id=run.id,
        id_sharing_operation=run.id_sharing_operation,
        period_start=run.period_start,
        period_end=run.period_end,
        status=run.status,
        regulator=run.regulator,
        invoice_count=invoice_count,
        created_at=run.created_at,
    )


def line_to_out(line: InvoiceLineModel) -> InvoiceLineOut:
    return InvoiceLineOut(
        id=line.id,
        ean=line.ean,
        direction=line.direction,
        measure=line.measure,
        quantity_kwh=line.quantity_kwh,
        unit_price=line.unit_price,
        amount=line.amount,
        description=line.description,
    )


def invoice_to_out(
    invoice: InvoiceModel, *, lines: list[InvoiceLineModel] | None = None
) -> InvoiceOut:
    return InvoiceOut(
        id=invoice.id,
        id_billing_run=invoice.id_billing_run,
        id_member=invoice.id_member,
        type=invoice.doc_type,
        status=invoice.status,
        number=invoice.number,
        currency=invoice.currency,
        subtotal=invoice.subtotal,
        vat_rate=invoice.vat_rate,
        vat_amount=invoice.vat_amount,
        total=invoice.total,
        structured_comm=invoice.structured_comm,
        issued_at=invoice.issued_at,
        due_date=invoice.due_date,
        pdf_ready=invoice.artifact_uri is not None,
        lines=[line_to_out(line_row) for line_row in lines] if lines is not None else None,
    )


def _format_address(obj: CommunityIdentity | ParticipantContact | None) -> str | None:
    if obj is None:
        return None
    # Coerce each part to str: address components (e.g. a house number) may arrive
    # as ints from the CRM, and a TypeError here would dead-letter the whole issue.
    line1 = " ".join(str(part) for part in (obj.street, obj.number) if part)
    line2 = " ".join(str(part) for part in (obj.postcode, obj.city) if part)
    joined = ", ".join(str(part) for part in (line1, line2, obj.supplement) if part)
    return joined or None


def build_docgen_data(
    *,
    invoice: InvoiceModel,
    lines: list[InvoiceLineModel],
    identity: CommunityIdentity,
    contact: ParticipantContact | None,
    regime: BillingRegime,
    locale: str,
) -> dict:
    """Assemble the JSON the invoice template renders (must satisfy its required_fields)."""
    return {
        "invoice": {
            "number": invoice.number,
            "type": int(invoice.doc_type),
            "issue_date": invoice.issued_at.date().isoformat() if invoice.issued_at else None,
            "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
            "currency": invoice.currency,
            "subtotal": str(invoice.subtotal),
            "vat_rate": str(invoice.vat_rate),
            "vat_amount": str(invoice.vat_amount),
            "total": str(invoice.total),
            "structured_comm": invoice.structured_comm,
        },
        "seller": {
            "legal_name": identity.legal_name,
            "vat_number": identity.vat_number,
            "iban": identity.iban,
            "account_holder_name": identity.account_holder_name or identity.legal_name,
            "address": _format_address(identity),
        },
        "buyer": {
            "name": contact.name if contact else None,
            "vat_number": contact.vat_number if contact else None,
            "email": contact.email if contact else None,
            "iban": contact.iban if contact else None,
            "address": _format_address(contact) if contact else None,
        },
        "lines": [
            {
                "ean": line.ean,
                "description": line.description,
                "quantity_kwh": str(line.quantity_kwh),
                "unit_price": str(line.unit_price),
                "amount": str(line.amount),
            }
            for line in lines
        ],
        "legal_mentions": regime.legal_mentions(locale=locale),
        "locale": locale,
    }


def payment_to_out(payment: PaymentModel) -> PaymentOut:
    return PaymentOut(
        id=payment.id,
        amount=payment.amount,
        currency=payment.currency,
        method=payment.method,
        reference=payment.reference,
        paid_at=payment.paid_at,
    )
