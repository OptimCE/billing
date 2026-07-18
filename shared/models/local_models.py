"""Owned billing tables (LOCAL database).

Mirrors scripts/sql/schema.sql, which is the single source of truth for DDL
(constraints, partial indexes, triggers). When changing a model, update the SQL
file and add a migration under scripts/sql/migrations/.

Multi-tenancy: every table carries a denormalised ``id_community`` int so the
``with_community_scope`` guard can filter without a cross-DB join. References
into the CRM core (``id_sharing_operation``, ``id_member``, ``ean``) are plain
columns — never foreign keys — because the CRM lives in a separate database.
"""

import datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database.database import LocalBase


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class _TimestampMixin:
    """created_at + updated_at for mutable tables (updated_at bumped by trigger)."""

    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class TariffModel(_TimestampMixin, LocalBase):
    """A community-set price for one axis (kind) at one scope, for a period.

    ``price_per_kwh`` is a free field. Resolution is most-specific-wins
    EAN → SEGMENT → GLOBAL, independently per ``kind`` (see BillingRepository).
    """

    __tablename__ = "tariff"
    __table_args__ = (
        Index("ix_tariff_lookup", "id_community", "id_sharing_operation", "kind", "scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    id_sharing_operation: Mapped[int] = mapped_column(Integer, nullable=False)

    kind: Mapped[int] = mapped_column(Integer, nullable=False)  # TariffKind
    scope: Mapped[int] = mapped_column(Integer, nullable=False)  # TariffScope
    scope_segment: Mapped[int | None] = mapped_column(Integer, nullable=True)  # client_type
    scope_ean: Mapped[str | None] = mapped_column(String(64), nullable=True)

    price_per_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    valid_from: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)


class BillingRunModel(_TimestampMixin, LocalBase):
    """One priced run over a (sharing operation, period). Idempotent per period."""

    __tablename__ = "billing_run"
    __table_args__ = (
        UniqueConstraint(
            "id_community",
            "id_sharing_operation",
            "period_start",
            "period_end",
            name="uq_billing_run_op_period",
        ),
        Index("ix_billing_run_status", "id_community", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    id_sharing_operation: Mapped[int] = mapped_column(Integer, nullable=False)

    period_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    period_end: Mapped[datetime.date] = mapped_column(Date, nullable=False)

    status: Mapped[int] = mapped_column(Integer, nullable=False)  # BillingRunStatus
    regulator: Mapped[str] = mapped_column(String(32), nullable=False)
    kwh_scale: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal(1))
    warnings: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    snapshots: Mapped[list["SettlementSnapshotModel"]] = relationship(
        back_populates="billing_run",
        cascade="all, delete-orphan",
        lazy="select",
    )
    invoices: Mapped[list["InvoiceModel"]] = relationship(
        back_populates="billing_run",
        lazy="select",
    )


class SettlementSnapshotModel(LocalBase):
    """Frozen per-(EAN, member) volumes for a run — the reproducible pricing input.

    A meter that changed owner mid-period yields one row per owner; the
    NULL-member row holds orphan volume (readings covered by no ownership
    window), which is never invoiced.
    """

    __tablename__ = "settlement_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "id_billing_run",
            "ean",
            "direction",
            "id_member",
            name="uq_settlement_snapshot_run_ean_dir_member",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    id_billing_run: Mapped[int] = mapped_column(
        Integer, ForeignKey("billing_run.id", ondelete="CASCADE"), nullable=False
    )

    ean: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[int] = mapped_column(Integer, nullable=False)  # BillingDirection
    client_type: Mapped[int | None] = mapped_column(Integer, nullable=True)  # segment, frozen

    shared_kwh: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal(0))
    inj_shared_kwh: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=Decimal(0)
    )
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    distinct_ts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    id_member: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Ownership window clamped to the run period; only set when it is a strict
    # subset of the period (drives the invoice-line date-range suffix).
    owned_from: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    owned_to: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )

    billing_run: Mapped["BillingRunModel"] = relationship(back_populates="snapshots")


class InvoiceModel(_TimestampMixin, LocalBase):
    """An immutable-once-issued billing document (invoice / credit note / statement)."""

    __tablename__ = "invoice"
    __table_args__ = (
        # Gapless numbering: at most one row per (legal entity, number) once
        # numbered. Partial so DRAFTs (number IS NULL) don't collide.
        Index(
            "uq_invoice_number",
            "legal_entity_key",
            "number",
            unique=True,
            postgresql_where=text("number IS NOT NULL"),
        ),
        Index("ix_invoice_status", "id_community", "status"),
        Index("ix_invoice_issued", "id_community", "issued_at"),
        Index("ix_invoice_run", "id_billing_run"),
        Index("ix_invoice_docgen", "docgen_request_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    id_billing_run: Mapped[int] = mapped_column(
        Integer, ForeignKey("billing_run.id"), nullable=False
    )
    id_member: Mapped[int] = mapped_column(Integer, nullable=False)

    doc_type: Mapped[int] = mapped_column("type", Integer, nullable=False)  # InvoiceType
    status: Mapped[int] = mapped_column(Integer, nullable=False)  # InvoiceStatus
    number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    legal_entity_key: Mapped[str] = mapped_column(String(64), nullable=False)

    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    subtotal: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal(0))
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False, default=Decimal(0))
    vat_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal(0))
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal(0))
    structured_comm: Mapped[str | None] = mapped_column(String(20), nullable=True)

    issued_at: Mapped[datetime.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    due_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    sent_at: Mapped[datetime.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    paid_at: Mapped[datetime.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    corrects_invoice_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artifact_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    docgen_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    billing_run: Mapped["BillingRunModel"] = relationship(back_populates="invoices")
    lines: Mapped[list["InvoiceLineModel"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        lazy="select",
    )
    payments: Mapped[list["PaymentModel"]] = relationship(
        back_populates="invoice",
        lazy="select",
    )


class InvoiceLineModel(LocalBase):
    """One priced line: an EAN's frozen volume x its resolved unit price."""

    __tablename__ = "invoice_line"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    id_invoice: Mapped[int] = mapped_column(
        Integer, ForeignKey("invoice.id", ondelete="CASCADE"), nullable=False
    )

    ean: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[int] = mapped_column(Integer, nullable=False)  # BillingDirection
    measure: Mapped[int] = mapped_column(Integer, nullable=False)  # Measure
    quantity_kwh: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )

    invoice: Mapped["InvoiceModel"] = relationship(back_populates="lines")


class CreditNoteModel(LocalBase):
    """Audit link between a corrected invoice and the credit note that voids it."""

    __tablename__ = "credit_note"
    __table_args__ = (
        UniqueConstraint(
            "id_original_invoice", "id_credit_invoice", name="uq_credit_note_pair"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    id_original_invoice: Mapped[int] = mapped_column(
        Integer, ForeignKey("invoice.id"), nullable=False
    )
    id_credit_invoice: Mapped[int] = mapped_column(
        Integer, ForeignKey("invoice.id"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )


class PaymentModel(LocalBase):
    """A payment recorded against an invoice. Invoices stay immutable."""

    __tablename__ = "payment"
    __table_args__ = (Index("ix_payment_invoice", "id_invoice"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    id_invoice: Mapped[int] = mapped_column(Integer, ForeignKey("invoice.id"), nullable=False)

    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    method: Mapped[int] = mapped_column(Integer, nullable=False)  # PaymentMethod
    reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paid_at: Mapped[datetime.datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)

    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )

    invoice: Mapped["InvoiceModel"] = relationship(back_populates="payments")


class InvoiceSequenceModel(LocalBase):
    """Per (community, legal entity, year, series) gapless counter.

    ``last_value`` is incremented atomically under SELECT ... FOR UPDATE at issue
    time. The ``legal_entity_key`` encodes both the legal entity and the document
    series (e.g. ``community:12:INV`` / ``:CN`` / ``:PS``) so each series numbers
    independently and gaplessly.
    """

    __tablename__ = "invoice_sequence"
    __table_args__ = (
        UniqueConstraint(
            "id_community", "legal_entity_key", "year", name="uq_invoice_sequence_scope"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_community: Mapped[int] = mapped_column(Integer, nullable=False)
    legal_entity_key: Mapped[str] = mapped_column(String(64), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    last_value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    updated_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
