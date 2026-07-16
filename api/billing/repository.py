"""Owned-DB data access for billing.

Every SELECT is tenant-scoped via ``with_community_scope`` (reads the
``current_internal_community_id`` ContextVar set by the request dependency or the
worker's ``with_tenant``). INSERTs stamp ``id_community`` from the same context.
The worker's ``get_run_by_id`` is the one deliberately UN-scoped read: it fetches
the owning row by PK to discover the tenant before context is set.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select, text, true, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from core.context_vars import current_internal_community_id
from core.database.with_community import with_community_scope
from shared.const import BillingRunStatus, InvoiceStatus, TariffScope
from shared.models.local_models import (
    BillingRunModel,
    CreditNoteModel,
    InvoiceLineModel,
    InvoiceModel,
    PaymentModel,
    SettlementSnapshotModel,
    TariffModel,
)

# Allow-list mapping the public ``sort`` value → a real column. NEVER interpolate
# the raw ``sort`` string into SQL: an unknown/None value falls back to ``id``.
_INVOICE_SORT_COLUMNS: dict[str, InstrumentedAttribute[Any]] = {
    "id": InvoiceModel.id,
    "issued_at": InvoiceModel.issued_at,
    "due_date": InvoiceModel.due_date,
    "total": InvoiceModel.total,
    "number": InvoiceModel.number,
    "status": InvoiceModel.status,
}


def _invoice_order_by(sort: str | None, order: str | None) -> list[Any]:
    """Build a safe ORDER BY for the invoice list endpoints.

    ``sort`` is resolved through the allow-list (unknown → ``id``); ``order`` is
    ``asc`` only when explicitly requested, else ``desc``. ``issued_at`` /
    ``due_date`` / ``number`` are nullable so NULLs sort last, and ``id`` is
    always appended as a stable tiebreaker.
    """
    column = _INVOICE_SORT_COLUMNS.get(sort or "id", InvoiceModel.id)
    direction = column.asc() if order == "asc" else column.desc()
    return [direction.nulls_last(), InvoiceModel.id.desc()]


class BillingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _community(self) -> int:
        cid = current_internal_community_id.get()
        if cid is None:
            raise RuntimeError("billing repository used without a community in context")
        return cid

    # ---- tariffs -----------------------------------------------------------
    async def create_tariff(self, tariff: TariffModel) -> TariffModel:
        tariff.id_community = self._community()
        self._session.add(tariff)
        await self._session.flush()
        return tariff

    async def list_tariffs(self, *, id_sharing_operation: int) -> list[TariffModel]:
        stmt = (
            with_community_scope(select(TariffModel), TariffModel)
            .where(TariffModel.id_sharing_operation == id_sharing_operation)
            .order_by(TariffModel.kind, TariffModel.scope, TariffModel.valid_from.desc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_tariff(self, tariff_id: int) -> TariffModel | None:
        stmt = with_community_scope(select(TariffModel), TariffModel).where(
            TariffModel.id == tariff_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def delete_tariff(self, tariff: TariffModel) -> None:
        await self._session.delete(tariff)
        await self._session.flush()

    async def has_global_tariff(
        self, *, id_sharing_operation: int, kind: int, on_date: datetime.date
    ) -> bool:
        stmt = (
            with_community_scope(select(TariffModel.id), TariffModel)
            .where(
                TariffModel.id_sharing_operation == id_sharing_operation,
                TariffModel.kind == kind,
                TariffModel.scope == TariffScope.GLOBAL,
                TariffModel.valid_from <= on_date,
                or_(TariffModel.valid_to.is_(None), TariffModel.valid_to >= on_date),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def resolve_price(
        self,
        *,
        id_sharing_operation: int,
        kind: int,
        ean: str,
        client_type: int | None,
        on_date: datetime.date,
    ) -> TariffModel | None:
        """Most-specific-wins: EAN → SEGMENT → GLOBAL, for the given kind + date."""
        candidates = [
            (TariffScope.EAN, TariffModel.scope_ean == ean),
            (TariffScope.SEGMENT, TariffModel.scope_segment == client_type),
            (TariffScope.GLOBAL, true()),
        ]
        for scope, extra in candidates:
            stmt = (
                with_community_scope(select(TariffModel), TariffModel)
                .where(
                    TariffModel.id_sharing_operation == id_sharing_operation,
                    TariffModel.kind == kind,
                    TariffModel.scope == scope,
                    TariffModel.valid_from <= on_date,
                    or_(TariffModel.valid_to.is_(None), TariffModel.valid_to >= on_date),
                    extra,
                )
                .order_by(TariffModel.valid_from.desc())
                .limit(1)
            )
            row = (await self._session.execute(stmt)).scalar_one_or_none()
            if row is not None:
                return row
        return None

    # ---- billing runs ------------------------------------------------------
    async def create_run(
        self,
        *,
        id_sharing_operation: int,
        period_start: datetime.date,
        period_end: datetime.date,
        regulator: str,
        kwh_scale: Decimal,
    ) -> BillingRunModel:
        run = BillingRunModel(
            id_community=self._community(),
            id_sharing_operation=id_sharing_operation,
            period_start=period_start,
            period_end=period_end,
            status=BillingRunStatus.PENDING,
            regulator=regulator,
            kwh_scale=kwh_scale,
        )
        self._session.add(run)
        await self._session.flush()
        return run

    async def get_run(self, run_id: int) -> BillingRunModel | None:
        stmt = with_community_scope(select(BillingRunModel), BillingRunModel).where(
            BillingRunModel.id == run_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_run_by_id(self, run_id: int) -> BillingRunModel | None:
        """UN-scoped fetch by PK — the worker uses this to discover the tenant."""
        return await self._session.get(BillingRunModel, run_id)

    async def list_runs(self, *, id_sharing_operation: int) -> list[BillingRunModel]:
        stmt = (
            with_community_scope(select(BillingRunModel), BillingRunModel)
            .where(BillingRunModel.id_sharing_operation == id_sharing_operation)
            .order_by(BillingRunModel.created_at.desc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def claim_run_computing(self, run_id: int) -> bool:
        """Flip PENDING/COMPUTING → COMPUTING atomically. rowcount is the lock."""
        result = await self._session.execute(
            update(BillingRunModel)
            .where(
                BillingRunModel.id == run_id,
                BillingRunModel.status.in_(
                    [BillingRunStatus.PENDING, BillingRunStatus.COMPUTING]
                ),
            )
            .values(status=BillingRunStatus.COMPUTING)
        )
        return bool(result.rowcount)

    async def set_run_status(
        self, run_id: int, status: BillingRunStatus, *, error_message: str | None = None
    ) -> None:
        await self._session.execute(
            update(BillingRunModel)
            .where(BillingRunModel.id == run_id)
            .values(status=status, error_message=error_message)
        )

    # ---- snapshots ---------------------------------------------------------
    async def add_snapshots(self, snapshots: list[SettlementSnapshotModel]) -> None:
        self._session.add_all(snapshots)
        await self._session.flush()

    async def list_snapshots(self, run_id: int) -> list[SettlementSnapshotModel]:
        stmt = with_community_scope(
            select(SettlementSnapshotModel), SettlementSnapshotModel
        ).where(SettlementSnapshotModel.id_billing_run == run_id)
        return list((await self._session.execute(stmt)).scalars().all())

    # ---- invoices ----------------------------------------------------------
    async def add_invoices(self, invoices: list[InvoiceModel]) -> None:
        self._session.add_all(invoices)
        await self._session.flush()

    async def get_invoice_by_id(self, invoice_id: int) -> InvoiceModel | None:
        """UN-scoped fetch by PK — the worker uses this to discover the tenant."""
        return await self._session.get(InvoiceModel, invoice_id)

    async def find_invoice_by_docgen_request(self, request_id: str) -> InvoiceModel | None:
        stmt = with_community_scope(select(InvoiceModel), InvoiceModel).where(
            InvoiceModel.docgen_request_id == request_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def claim_next_number(self, *, legal_entity_key: str, year: int) -> int:
        """Atomically allocate the next gapless value for a (community, series, year).

        INSERT-if-absent then ``UPDATE … RETURNING`` — the per-row lock the UPDATE
        takes serialises concurrent issues, so numbers are contiguous and unique.
        """
        params = {"cid": self._community(), "lek": legal_entity_key, "year": year}
        await self._session.execute(
            text(
                """
                INSERT INTO invoice_sequence (id_community, legal_entity_key, year, last_value)
                VALUES (:cid, :lek, :year, 0)
                ON CONFLICT (id_community, legal_entity_key, year) DO NOTHING
                """
            ),
            params,
        )
        result = await self._session.execute(
            text(
                """
                UPDATE invoice_sequence
                SET last_value = last_value + 1, updated_at = now()
                WHERE id_community = :cid AND legal_entity_key = :lek AND year = :year
                RETURNING last_value
                """
            ),
            params,
        )
        return int(result.scalar_one())

    async def mark_issued(
        self,
        invoice_id: int,
        *,
        number: str,
        structured_comm: str,
        issued_at: datetime.datetime,
        due_date: datetime.date,
    ) -> None:
        # Also drop any pre-existing artifact (e.g. a DRAFT proforma PDF): the
        # issue-time render must regenerate the numbered/legal PDF, and
        # process_issue no-ops when artifact_uri is already set.
        await self._session.execute(
            update(InvoiceModel)
            .where(InvoiceModel.id == invoice_id)
            .values(
                number=number,
                structured_comm=structured_comm,
                status=InvoiceStatus.ISSUED,
                issued_at=issued_at,
                due_date=due_date,
                artifact_uri=None,
                artifact_sha256=None,
                docgen_request_id=None,
            )
        )

    async def set_docgen_request_id(self, invoice_id: int, request_id: str) -> None:
        await self._session.execute(
            update(InvoiceModel)
            .where(InvoiceModel.id == invoice_id)
            .values(docgen_request_id=request_id)
        )

    async def attach_artifact(
        self,
        invoice_id: int,
        *,
        uri: str,
        sha256: str | None,
        set_status: InvoiceStatus | None = None,
    ) -> None:
        values: dict[str, object] = {"artifact_uri": uri, "artifact_sha256": sha256}
        if set_status is not None:
            values["status"] = set_status
        await self._session.execute(
            update(InvoiceModel).where(InvoiceModel.id == invoice_id).values(**values)
        )

    async def clear_artifact(
        self, invoice_id: int, *, reset_status_to: InvoiceStatus | None = None
    ) -> None:
        """Drop the rendered-PDF reference so a fresh render is produced.

        Nulls ``artifact_uri`` / ``artifact_sha256`` / ``docgen_request_id`` so
        ``process_issue`` re-renders with a new key. ``reset_status_to`` optionally
        moves the status back (e.g. RENDER_FAILED → ISSUED before a re-render).
        """
        values: dict[str, object | None] = {
            "artifact_uri": None,
            "artifact_sha256": None,
            "docgen_request_id": None,
        }
        if reset_status_to is not None:
            values["status"] = reset_status_to
        await self._session.execute(
            update(InvoiceModel).where(InvoiceModel.id == invoice_id).values(**values)
        )

    async def mark_render_failed(self, invoice_id: int) -> None:
        await self._session.execute(
            update(InvoiceModel)
            .where(InvoiceModel.id == invoice_id)
            .values(status=InvoiceStatus.RENDER_FAILED)
        )

    async def mark_sent(self, invoice_id: int, sent_at: datetime.datetime) -> None:
        await self._session.execute(
            update(InvoiceModel)
            .where(InvoiceModel.id == invoice_id)
            .values(status=InvoiceStatus.SENT, sent_at=sent_at)
        )

    async def mark_paid(self, invoice_id: int, paid_at: datetime.datetime) -> None:
        await self._session.execute(
            update(InvoiceModel)
            .where(InvoiceModel.id == invoice_id)
            .values(status=InvoiceStatus.PAID, paid_at=paid_at)
        )

    async def sweep_overdue(self, today: datetime.date) -> int:
        """Mark community-scoped ISSUED/SENT invoices past due as OVERDUE. Returns count."""
        result = await self._session.execute(
            update(InvoiceModel)
            .where(
                InvoiceModel.id_community == self._community(),
                InvoiceModel.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.SENT]),
                InvoiceModel.due_date < today,
            )
            .values(status=InvoiceStatus.OVERDUE)
        )
        return int(result.rowcount)

    # ---- payments ----------------------------------------------------------
    async def add_payment(self, payment: PaymentModel) -> PaymentModel:
        payment.id_community = self._community()
        self._session.add(payment)
        await self._session.flush()
        return payment

    async def list_payments(self, invoice_id: int) -> list[PaymentModel]:
        stmt = (
            with_community_scope(select(PaymentModel), PaymentModel)
            .where(PaymentModel.id_invoice == invoice_id)
            .order_by(PaymentModel.id)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def sum_payments(self, invoice_id: int) -> Decimal:
        stmt = with_community_scope(
            select(func.coalesce(func.sum(PaymentModel.amount), 0)), PaymentModel
        ).where(PaymentModel.id_invoice == invoice_id)
        return Decimal(str((await self._session.execute(stmt)).scalar_one()))

    # ---- credit notes ------------------------------------------------------
    async def add_credit_note_link(self, link: CreditNoteModel) -> None:
        link.id_community = self._community()
        self._session.add(link)
        await self._session.flush()

    async def count_invoices_for_run(self, run_id: int) -> int:
        stmt = with_community_scope(
            select(func.count()).select_from(InvoiceModel), InvoiceModel
        ).where(InvoiceModel.id_billing_run == run_id)
        return int((await self._session.execute(stmt)).scalar_one())

    async def list_invoices_for_run(self, run_id: int) -> list[InvoiceModel]:
        stmt = (
            with_community_scope(select(InvoiceModel), InvoiceModel)
            .where(InvoiceModel.id_billing_run == run_id)
            .order_by(InvoiceModel.id)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_invoice(self, invoice_id: int) -> InvoiceModel | None:
        stmt = with_community_scope(select(InvoiceModel), InvoiceModel).where(
            InvoiceModel.id == invoice_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_invoice_lines(self, invoice_id: int) -> list[InvoiceLineModel]:
        stmt = (
            with_community_scope(select(InvoiceLineModel), InvoiceLineModel)
            .where(InvoiceLineModel.id_invoice == invoice_id)
            .order_by(InvoiceLineModel.id)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_invoices(
        self,
        *,
        status: int | None = None,
        id_member: int | None = None,
        limit: int = 50,
        offset: int = 0,
        issued_from: datetime.date | None = None,
        issued_to: datetime.date | None = None,
        sort: str | None = None,
        order: str | None = None,
    ) -> tuple[list[InvoiceModel], int]:
        base = with_community_scope(select(InvoiceModel), InvoiceModel)
        count_base = with_community_scope(
            select(func.count()).select_from(InvoiceModel), InvoiceModel
        )
        if status is not None:
            base = base.where(InvoiceModel.status == status)
            count_base = count_base.where(InvoiceModel.status == status)
        if id_member is not None:
            base = base.where(InvoiceModel.id_member == id_member)
            count_base = count_base.where(InvoiceModel.id_member == id_member)
        if issued_from is not None:
            base = base.where(InvoiceModel.issued_at >= issued_from)
            count_base = count_base.where(InvoiceModel.issued_at >= issued_from)
        if issued_to is not None:
            # Inclusive of the whole ``issued_to`` day (issued_at is a timestamp).
            upper = issued_to + datetime.timedelta(days=1)
            base = base.where(InvoiceModel.issued_at < upper)
            count_base = count_base.where(InvoiceModel.issued_at < upper)

        total = int((await self._session.execute(count_base)).scalar_one())
        rows = (
            await self._session.execute(
                base.order_by(*_invoice_order_by(sort, order)).limit(limit).offset(offset)
            )
        ).scalars().all()
        return list(rows), total

    async def list_invoices_for_members(
        self,
        *,
        id_members: list[int],
        status: int | None = None,
        limit: int = 50,
        offset: int = 0,
        issued_from: datetime.date | None = None,
        issued_to: datetime.date | None = None,
        sort: str | None = None,
        order: str | None = None,
    ) -> tuple[list[InvoiceModel], int]:
        """Community-scoped invoices restricted to a set of members (caller-scoped view)."""
        base = with_community_scope(select(InvoiceModel), InvoiceModel).where(
            InvoiceModel.id_member.in_(id_members)
        )
        count_base = with_community_scope(
            select(func.count()).select_from(InvoiceModel), InvoiceModel
        ).where(InvoiceModel.id_member.in_(id_members))
        if status is not None:
            base = base.where(InvoiceModel.status == status)
            count_base = count_base.where(InvoiceModel.status == status)
        if issued_from is not None:
            base = base.where(InvoiceModel.issued_at >= issued_from)
            count_base = count_base.where(InvoiceModel.issued_at >= issued_from)
        if issued_to is not None:
            # Inclusive of the whole ``issued_to`` day (issued_at is a timestamp).
            upper = issued_to + datetime.timedelta(days=1)
            base = base.where(InvoiceModel.issued_at < upper)
            count_base = count_base.where(InvoiceModel.issued_at < upper)

        total = int((await self._session.execute(count_base)).scalar_one())
        rows = (
            await self._session.execute(
                base.order_by(*_invoice_order_by(sort, order)).limit(limit).offset(offset)
            )
        ).scalars().all()
        return list(rows), total
