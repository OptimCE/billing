"""Pydantic request/response DTOs for the billing API."""

from __future__ import annotations

import datetime
from decimal import Decimal

from pydantic import BaseModel, model_validator

from shared.const import PaymentMethod, TariffKind, TariffScope


class TariffIn(BaseModel):
    kind: int  # TariffKind
    scope: int  # TariffScope
    scope_segment: int | None = None  # client_type when scope=SEGMENT
    scope_ean: str | None = None  # EAN when scope=EAN
    price_per_kwh: Decimal
    currency: str = "EUR"
    valid_from: datetime.date
    valid_to: datetime.date | None = None
    label: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> TariffIn:
        if self.kind not in (TariffKind.CONSUMER_SELLING, TariffKind.PRODUCER_BUYBACK):
            raise ValueError("kind must be 1 (CONSUMER_SELLING) or 2 (PRODUCER_BUYBACK)")
        if self.scope not in (TariffScope.GLOBAL, TariffScope.SEGMENT, TariffScope.EAN):
            raise ValueError("scope must be 1 (GLOBAL), 2 (SEGMENT) or 3 (EAN)")
        if self.scope == TariffScope.SEGMENT and self.scope_segment is None:
            raise ValueError("scope_segment is required for SEGMENT scope")
        if self.scope == TariffScope.EAN and not self.scope_ean:
            raise ValueError("scope_ean is required for EAN scope")
        if self.scope == TariffScope.GLOBAL and (
            self.scope_segment is not None or self.scope_ean is not None
        ):
            raise ValueError("GLOBAL scope must not set scope_segment or scope_ean")
        if self.price_per_kwh < 0:
            raise ValueError("price_per_kwh must be non-negative")
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to must be on or after valid_from")
        return self


class TariffOut(BaseModel):
    id: int
    id_sharing_operation: int
    kind: int
    scope: int
    scope_segment: int | None
    scope_ean: str | None
    price_per_kwh: Decimal
    currency: str
    valid_from: datetime.date
    valid_to: datetime.date | None
    label: str | None


class BillingRunRequest(BaseModel):
    period_start: datetime.date
    period_end: datetime.date

    @model_validator(mode="after")
    def _validate(self) -> BillingRunRequest:
        if self.period_end < self.period_start:
            raise ValueError("period_end must be on or after period_start")
        return self


class BillingRunOut(BaseModel):
    id: int
    id_sharing_operation: int
    period_start: datetime.date
    period_end: datetime.date
    status: int
    regulator: str
    invoice_count: int | None = None
    warnings: list[dict] | None = None
    created_at: datetime.datetime | None = None


class InvoiceLineOut(BaseModel):
    id: int
    ean: str
    direction: int
    measure: int
    quantity_kwh: Decimal
    unit_price: Decimal
    amount: Decimal
    description: str | None


class InvoiceOut(BaseModel):
    id: int
    id_billing_run: int
    id_member: int
    type: int
    status: int
    number: str | None
    currency: str
    subtotal: Decimal
    vat_rate: Decimal
    vat_amount: Decimal
    total: Decimal
    structured_comm: str | None
    issued_at: datetime.datetime | None
    due_date: datetime.date | None
    pdf_ready: bool = False
    lines: list[InvoiceLineOut] | None = None


class PdfRenderIn(BaseModel):
    # force re-render even when a PDF already exists (regenerate).
    force: bool = False


class RenderRequestOut(BaseModel):
    id: int
    status: int
    pdf_ready: bool


class IssueOut(BaseModel):
    id: int
    number: str
    status: int
    due_date: datetime.date | None
    structured_comm: str | None


class PaymentIn(BaseModel):
    amount: Decimal
    method: int = int(PaymentMethod.BANK_TRANSFER)
    reference: str | None = None
    paid_on: datetime.date | None = None

    @model_validator(mode="after")
    def _validate(self) -> PaymentIn:
        if self.amount <= 0:
            raise ValueError("amount must be positive")
        return self


class PaymentOut(BaseModel):
    id: int
    amount: Decimal
    currency: str
    method: int
    reference: str | None
    paid_at: datetime.datetime


class CreditNoteIn(BaseModel):
    reason: str | None = None


class OverdueSweepOut(BaseModel):
    marked: int
