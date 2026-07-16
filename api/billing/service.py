"""Billing orchestration: tariffs, and the billing-run pre-flight + snapshot."""

from __future__ import annotations

import datetime
import logging
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.billing import mappers
from api.billing.repository import BillingRepository
from api.billing.schemas import (
    BillingRunOut,
    BillingRunRequest,
    CreditNoteIn,
    InvoiceOut,
    IssueOut,
    OverdueSweepOut,
    PaymentIn,
    PaymentOut,
    RenderRequestOut,
    TariffIn,
    TariffOut,
)
from core import metrics as app_metrics
from core.audit_log import AuditActions, AuditLogInput, AuditLogService
from core.config import Settings
from core.context_vars import (
    current_internal_community_id,
    current_user_id,
    current_user_role,
)
from core.errors.errors import ErrorException
from core.queue.helper import Event
from core.security.user_context import ROLE_HIERARCHY, Role
from core.storage import client as storage
from ports.crm_core import CrmCoreReadPort
from ports.email import EmailMessage, EmailPort
from ports.events import EventPublisher
from regime.registry import RegimeConfigError, RegimeRegistry
from shared.const import (
    SUBJECT_INVOICE_ISSUE_REQUESTED,
    SUBJECT_RUN_REQUESTED,
    BillingDirection,
    BillingRunStatus,
    InvoiceStatus,
    InvoiceType,
    TariffKind,
)
from shared.custom_errors import errors
from shared.models.local_models import (
    CreditNoteModel,
    InvoiceLineModel,
    InvoiceModel,
    PaymentModel,
    SettlementSnapshotModel,
    TariffModel,
)
from utils import ogm

logger = logging.getLogger(__name__)

_SETTLEMENT_TZ = ZoneInfo("Europe/Brussels")


class BillingService:
    def __init__(
        self,
        *,
        local_session: AsyncSession,
        crm_session: AsyncSession,
        repository: BillingRepository,
        crm_read: CrmCoreReadPort,
        registry: RegimeRegistry,
        publisher: EventPublisher,
        email: EmailPort,
        audit: AuditLogService,
        settings: Settings,
    ) -> None:
        self._local = local_session
        self._crm = crm_session
        self._repo = repository
        self._crm_read = crm_read
        self._registry = registry
        self._publisher = publisher
        self._email = email
        self._audit = audit
        self._settings = settings

    def _community(self) -> int:
        cid = current_internal_community_id.get()
        if cid is None:
            raise ErrorException(errors.auth.AUTHORIZATION_MISSING, status_code=401)
        return cid

    # ---- tariffs -----------------------------------------------------------
    async def create_tariff(self, *, id_sharing_operation: int, body: TariffIn) -> TariffOut:
        tariff = TariffModel(
            id_sharing_operation=id_sharing_operation,
            kind=body.kind,
            scope=body.scope,
            scope_segment=body.scope_segment,
            scope_ean=body.scope_ean,
            price_per_kwh=body.price_per_kwh,
            currency=body.currency,
            valid_from=body.valid_from,
            valid_to=body.valid_to,
            label=body.label,
        )
        try:
            await self._repo.create_tariff(tariff)
            await self._local.commit()
        except IntegrityError as exc:
            await self._local.rollback()
            raise ErrorException(errors.billing.INVALID_TARIFF, status_code=409) from exc
        return mappers.tariff_to_out(tariff)

    async def list_tariffs(self, *, id_sharing_operation: int) -> list[TariffOut]:
        rows = await self._repo.list_tariffs(id_sharing_operation=id_sharing_operation)
        return [mappers.tariff_to_out(row) for row in rows]

    async def delete_tariff(self, *, tariff_id: int) -> None:
        tariff = await self._repo.get_tariff(tariff_id)
        if tariff is None:
            raise ErrorException(errors.billing.TARIFF_NOT_FOUND, status_code=404)
        await self._repo.delete_tariff(tariff)
        await self._local.commit()

    # ---- billing run -------------------------------------------------------
    async def create_billing_run(
        self, *, id_sharing_operation: int, body: BillingRunRequest
    ) -> BillingRunOut:
        cid = self._community()
        period_start, period_end = body.period_start, body.period_end

        identity = await self._crm_read.get_community_identity(id_community=cid)
        if identity is None:
            raise ErrorException(
                errors.billing.COMMUNITY_BILLING_INFO_INCOMPLETE, status_code=422
            )
        try:
            self._registry.get_for(identity.regulator)
        except RegimeConfigError as exc:
            raise ErrorException(errors.billing.REGIME_NOT_CONFIGURED, status_code=500) from exc

        aggregates = await self._crm_read.aggregate_by_ean(
            id_community=cid,
            id_sharing_operation=id_sharing_operation,
            period_start=period_start,
            period_end=period_end,
        )
        if not aggregates:
            raise ErrorException(errors.billing.NO_CONSUMPTION_DATA, status_code=422)
        if any(agg.has_duplicate_rows for agg in aggregates):
            raise ErrorException(errors.billing.DOUBLE_IMPORT_DETECTED, status_code=422)
        if not identity.has_billing_info:
            raise ErrorException(
                errors.billing.COMMUNITY_BILLING_INFO_INCOMPLETE, status_code=422
            )

        has_consumer = any(agg.shared_sum > 0 for agg in aggregates)
        has_producer = any(agg.inj_shared_sum > 0 for agg in aggregates)
        if has_consumer and not await self._repo.has_global_tariff(
            id_sharing_operation=id_sharing_operation,
            kind=TariffKind.CONSUMER_SELLING,
            on_date=period_start,
        ):
            raise ErrorException(errors.billing.TARIFF_NOT_FOUND, status_code=422)
        if has_producer and not await self._repo.has_global_tariff(
            id_sharing_operation=id_sharing_operation,
            kind=TariffKind.PRODUCER_BUYBACK,
            on_date=period_start,
        ):
            raise ErrorException(errors.billing.TARIFF_NOT_FOUND, status_code=422)

        scale = Decimal(str(self._settings.KWH_SCALE))
        try:
            run = await self._repo.create_run(
                id_sharing_operation=id_sharing_operation,
                period_start=period_start,
                period_end=period_end,
                regulator=identity.regulator,
                kwh_scale=scale,
            )
        except IntegrityError as exc:
            await self._local.rollback()
            raise ErrorException(errors.billing.RUN_ALREADY_EXISTS, status_code=409) from exc

        active = await self._crm_read.active_eans(
            id_community=cid,
            id_sharing_operation=id_sharing_operation,
            period_start=period_start,
            period_end=period_end,
        )
        active_by_ean = {row.ean: row for row in active}

        snapshots: list[SettlementSnapshotModel] = []
        for agg in aggregates:
            member = active_by_ean.get(agg.ean)
            id_member = member.id_member if member else None
            client_type = member.client_type if member else None
            if agg.shared_sum > 0:
                snapshots.append(
                    SettlementSnapshotModel(
                        id_community=cid,
                        id_billing_run=run.id,
                        ean=agg.ean,
                        direction=BillingDirection.CONSUMER,
                        client_type=client_type,
                        shared_kwh=agg.shared_sum * scale,
                        inj_shared_kwh=Decimal(0),
                        row_count=agg.row_count,
                        distinct_ts_count=agg.distinct_ts,
                        id_member=id_member,
                    )
                )
            if agg.inj_shared_sum > 0:
                snapshots.append(
                    SettlementSnapshotModel(
                        id_community=cid,
                        id_billing_run=run.id,
                        ean=agg.ean,
                        direction=BillingDirection.PRODUCER,
                        client_type=client_type,
                        shared_kwh=Decimal(0),
                        inj_shared_kwh=agg.inj_shared_sum * scale,
                        row_count=agg.row_count,
                        distinct_ts_count=agg.distinct_ts,
                        id_member=id_member,
                    )
                )
        await self._repo.add_snapshots(snapshots)
        await self._local.commit()  # freeze run + snapshots

        try:
            await self._publisher.publish(
                SUBJECT_RUN_REQUESTED,
                Event(type="billing.run.requested", data={"billing_run_id": run.id}),
            )
        except Exception as exc:
            logger.exception("Failed to enqueue billing run %s", run.id)
            await self._repo.set_run_status(
                run.id, BillingRunStatus.FAILED, error_message="failed to enqueue"
            )
            await self._local.commit()
            raise ErrorException(errors.billing.START_BILLING_RUN, status_code=500) from exc

        await self._audit.log(
            AuditLogInput(
                action=AuditActions.RUN_CREATED,
                entity_type="billing_run",
                entity_id=str(run.id),
                payload={
                    "kwh_scale": str(scale),
                    "period_start": str(period_start),
                    "period_end": str(period_end),
                    "ean_count": len(aggregates),
                },
            ),
            id_community=cid,
        )
        await self._crm.commit()

        return mappers.run_to_out(run)

    # ---- issue -------------------------------------------------------------
    async def issue_invoice(self, *, invoice_id: int) -> IssueOut:
        cid = self._community()
        invoice = await self._repo.get_invoice(invoice_id)
        if invoice is None:
            raise ErrorException(errors.billing.INVOICE_NOT_FOUND, status_code=404)
        if invoice.status != InvoiceStatus.DRAFT:
            raise ErrorException(errors.billing.INVOICE_NOT_DRAFT, status_code=409)

        identity = await self._crm_read.get_community_identity(id_community=cid)
        if identity is None:
            raise ErrorException(
                errors.billing.COMMUNITY_BILLING_INFO_INCOMPLETE, status_code=422
            )
        try:
            regime = self._registry.get_for(identity.regulator)
        except RegimeConfigError as exc:
            raise ErrorException(errors.billing.REGIME_NOT_CONFIGURED, status_code=500) from exc

        now_utc = datetime.datetime.now(datetime.UTC)
        local = now_utc.astimezone(_SETTLEMENT_TZ)
        year = local.year
        try:
            seq = await self._repo.claim_next_number(
                legal_entity_key=invoice.legal_entity_key, year=year
            )
        except Exception as exc:
            await self._local.rollback()
            raise ErrorException(errors.billing.NUMBERING_FAILED, status_code=500) from exc

        number = regime.format_number(doc_type=InvoiceType(invoice.doc_type), year=year, seq=seq)
        structured_comm = ogm.generate(invoice.id)
        due_date = local.date() + datetime.timedelta(days=regime.due_days())

        await self._repo.mark_issued(
            invoice_id,
            number=number,
            structured_comm=structured_comm,
            issued_at=now_utc,
            due_date=due_date,
        )
        await self._local.commit()  # the number is the legal act; PDF render is retryable

        try:
            await self._publisher.publish(
                SUBJECT_INVOICE_ISSUE_REQUESTED,
                Event(type="billing.invoice.issue.requested", data={"invoice_id": invoice_id}),
            )
        except Exception:
            logger.exception(
                "Invoice %s issued as %s but PDF enqueue failed; render can be retried",
                invoice_id,
                number,
            )

        await self._audit.log(
            AuditLogInput(
                action=AuditActions.INVOICE_ISSUED,
                entity_type="invoice",
                entity_id=str(invoice_id),
                payload={"number": number},
            ),
            id_community=cid,
        )
        await self._crm.commit()
        app_metrics.invoices_issued.add(1)

        return IssueOut(
            id=invoice_id,
            number=number,
            status=int(InvoiceStatus.ISSUED),
            due_date=due_date,
            structured_comm=structured_comm,
        )

    # ---- send / payment / overdue / credit-note ----------------------------
    async def send_invoice(self, *, invoice_id: int) -> InvoiceOut:
        cid = self._community()
        invoice = await self._repo.get_invoice(invoice_id)
        if invoice is None:
            raise ErrorException(errors.billing.INVOICE_NOT_FOUND, status_code=404)
        if invoice.status not in (
            InvoiceStatus.ISSUED,
            InvoiceStatus.SENT,
            InvoiceStatus.OVERDUE,
        ):
            raise ErrorException(errors.billing.INVOICE_NOT_ISSUED, status_code=409)
        if not invoice.artifact_uri:
            raise ErrorException(errors.billing.INVOICE_PDF_NOT_READY, status_code=422)

        contacts = await self._crm_read.participant_contacts(
            id_community=cid, member_ids=[invoice.id_member]
        )
        contact = contacts.get(invoice.id_member)
        email = (contact.email if contact else None) or ""
        if not email.strip():
            raise ErrorException(errors.billing.NO_BILLING_EMAIL, status_code=422)

        await self._email.send(
            EmailMessage(
                to=email,
                subject=f"Facture {invoice.number}",
                body="Veuillez trouver votre facture en pièce jointe.",
                attachment_ref=invoice.artifact_uri,
            )
        )
        await self._repo.mark_sent(invoice_id, datetime.datetime.now(datetime.UTC))
        await self._local.commit()
        await self._audit.log(
            AuditLogInput(
                action=AuditActions.INVOICE_SENT,
                entity_type="invoice",
                entity_id=str(invoice_id),
                payload={"to": email},
            ),
            id_community=cid,
        )
        await self._crm.commit()
        return await self.get_invoice(invoice_id=invoice_id)

    async def register_payment(self, *, invoice_id: int, body: PaymentIn) -> InvoiceOut:
        cid = self._community()
        invoice = await self._repo.get_invoice(invoice_id)
        if invoice is None:
            raise ErrorException(errors.billing.INVOICE_NOT_FOUND, status_code=404)
        if invoice.status not in (
            InvoiceStatus.ISSUED,
            InvoiceStatus.SENT,
            InvoiceStatus.OVERDUE,
        ):
            raise ErrorException(errors.billing.INVOICE_NOT_ISSUED, status_code=409)

        paid_at = (
            datetime.datetime.combine(body.paid_on, datetime.time.min, tzinfo=_SETTLEMENT_TZ)
            if body.paid_on
            else datetime.datetime.now(datetime.UTC)
        )
        await self._repo.add_payment(
            PaymentModel(
                id_invoice=invoice_id,
                amount=body.amount,
                currency=invoice.currency,
                method=body.method,
                reference=body.reference,
                paid_at=paid_at,
            )
        )
        became_paid = await self._repo.sum_payments(invoice_id) >= invoice.total
        if became_paid:
            await self._repo.mark_paid(invoice_id, datetime.datetime.now(datetime.UTC))
        await self._local.commit()

        await self._audit.log(
            AuditLogInput(
                action=AuditActions.PAYMENT_REGISTERED,
                entity_type="invoice",
                entity_id=str(invoice_id),
                payload={"amount": str(body.amount), "settled": became_paid},
            ),
            id_community=cid,
        )
        if became_paid:
            await self._audit.log(
                AuditLogInput(
                    action=AuditActions.INVOICE_PAID,
                    entity_type="invoice",
                    entity_id=str(invoice_id),
                    payload={},
                ),
                id_community=cid,
            )
        await self._crm.commit()
        return await self.get_invoice(invoice_id=invoice_id)

    async def list_payments(self, *, invoice_id: int) -> list[PaymentOut]:
        invoice = await self._repo.get_invoice(invoice_id)
        if invoice is None:
            raise ErrorException(errors.billing.INVOICE_NOT_FOUND, status_code=404)
        payments = await self._repo.list_payments(invoice_id)
        return [mappers.payment_to_out(payment) for payment in payments]

    async def sweep_overdue(self) -> OverdueSweepOut:
        self._community()
        today = datetime.datetime.now(_SETTLEMENT_TZ).date()
        marked = await self._repo.sweep_overdue(today)
        await self._local.commit()
        return OverdueSweepOut(marked=marked)

    async def create_credit_note(self, *, invoice_id: int, body: CreditNoteIn) -> InvoiceOut:
        cid = self._community()
        original = await self._repo.get_invoice(invoice_id)
        if original is None:
            raise ErrorException(errors.billing.INVOICE_NOT_FOUND, status_code=404)
        # Only an issued consumer invoice can be credited (a DRAFT is deleted, not
        # credited; a credit note isn't itself creditable).
        if (
            original.doc_type != InvoiceType.INVOICE
            or original.status == InvoiceStatus.DRAFT
        ):
            raise ErrorException(errors.billing.CREDIT_NOTE_TARGET_INVALID, status_code=409)

        identity = await self._crm_read.get_community_identity(id_community=cid)
        if identity is None:
            raise ErrorException(
                errors.billing.COMMUNITY_BILLING_INFO_INCOMPLETE, status_code=422
            )
        try:
            regime = self._registry.get_for(identity.regulator)
        except RegimeConfigError as exc:
            raise ErrorException(errors.billing.REGIME_NOT_CONFIGURED, status_code=500) from exc

        lines = await self._repo.get_invoice_lines(invoice_id)
        credit_lines = [
            InvoiceLineModel(
                id_community=cid,
                ean=line.ean,
                direction=line.direction,
                measure=line.measure,
                quantity_kwh=-line.quantity_kwh,
                unit_price=line.unit_price,
                amount=-line.amount,
                description=f"Note de crédit — {line.description or ''}".strip(),
            )
            for line in lines
        ]
        credit_note = InvoiceModel(
            id_community=cid,
            id_billing_run=original.id_billing_run,
            id_member=original.id_member,
            doc_type=InvoiceType.CREDIT_NOTE,
            status=InvoiceStatus.DRAFT,
            legal_entity_key=f"community:{cid}:{regime.series_prefix(InvoiceType.CREDIT_NOTE)}",
            currency=original.currency,
            subtotal=-original.subtotal,
            vat_rate=original.vat_rate,
            vat_amount=-original.vat_amount,
            total=-original.total,
            corrects_invoice_id=original.id,
            lines=credit_lines,
        )
        await self._repo.add_invoices([credit_note])
        await self._repo.add_credit_note_link(
            CreditNoteModel(
                id_original_invoice=original.id,
                id_credit_invoice=credit_note.id,
                reason=body.reason,
            )
        )
        await self._local.commit()

        await self._audit.log(
            AuditLogInput(
                action=AuditActions.INVOICE_CREDITED,
                entity_type="invoice",
                entity_id=str(original.id),
                payload={"credit_invoice_id": credit_note.id},
            ),
            id_community=cid,
        )
        await self._crm.commit()
        return mappers.invoice_to_out(credit_note, lines=credit_lines)

    # ---- PDF generation / serving ------------------------------------------
    def _is_manager(self) -> bool:
        """True if the caller's active role is MANAGER or above (gateway-asserted)."""
        role_str = current_user_role.get()
        if not role_str:
            return False
        try:
            role = Role(role_str)
        except ValueError:
            return False
        return ROLE_HIERARCHY.get(role, 0) >= ROLE_HIERARCHY[Role.MANAGER]

    def _pdf_filename(self, invoice: InvoiceModel) -> str:
        """Human-friendly download name; DRAFT (no number) → proforma."""
        if invoice.number:
            safe = invoice.number.replace("/", "-").replace(" ", "")
            if invoice.doc_type == InvoiceType.CREDIT_NOTE:
                return f"note-credit-{safe}.pdf"
            if invoice.doc_type == InvoiceType.PRODUCER_STATEMENT:
                return f"decompte-{safe}.pdf"
            return f"facture-{safe}.pdf"
        return f"proforma-{invoice.id}.pdf"

    async def request_render(self, *, invoice_id: int, force: bool) -> RenderRequestOut:
        """Ask document-generation for (a fresh) PDF of an existing invoice.

        Reuses the issue→docgen worker path (``process_issue`` is the render step).
        ``force`` (or a prior RENDER_FAILED) clears the stored artifact so a new
        render key is used; a DRAFT renders a watermarked proforma.
        """
        cid = self._community()
        invoice = await self._repo.get_invoice(invoice_id)
        if invoice is None:
            raise ErrorException(errors.billing.INVOICE_NOT_FOUND, status_code=404)
        if invoice.status == InvoiceStatus.CANCELLED:
            raise ErrorException(errors.billing.INVOICE_NOT_RENDERABLE, status_code=409)

        was_failed = invoice.status == InvoiceStatus.RENDER_FAILED
        cleared = force or was_failed
        if cleared:
            await self._repo.clear_artifact(
                invoice_id, reset_status_to=InvoiceStatus.ISSUED if was_failed else None
            )
            await self._local.commit()

        try:
            await self._publisher.publish(
                SUBJECT_INVOICE_ISSUE_REQUESTED,
                Event(type="billing.invoice.issue.requested", data={"invoice_id": invoice_id}),
            )
        except Exception as exc:
            logger.exception("Failed to enqueue PDF render for invoice %s", invoice_id)
            raise ErrorException(errors.billing.ISSUE_INVOICE, status_code=500) from exc

        await self._audit.log(
            AuditLogInput(
                action=AuditActions.INVOICE_RENDER_REQUESTED,
                entity_type="invoice",
                entity_id=str(invoice_id),
                payload={"force": force},
            ),
            id_community=cid,
        )
        await self._crm.commit()

        status_after = InvoiceStatus.ISSUED if was_failed else InvoiceStatus(invoice.status)
        pdf_ready = (not cleared) and invoice.artifact_uri is not None
        return RenderRequestOut(id=invoice_id, status=int(status_after), pdf_ready=pdf_ready)

    async def download_invoice_pdf(self, *, invoice_id: int) -> tuple[bytes, str]:
        """Return the rendered PDF bytes + a download filename.

        Managers may fetch any invoice in the community; a plain member may only
        fetch a PDF of an invoice belonging to one of their own member records.
        """
        cid = self._community()
        invoice = await self._repo.get_invoice(invoice_id)
        if invoice is None:
            raise ErrorException(errors.billing.INVOICE_NOT_FOUND, status_code=404)

        if not self._is_manager():
            auth_user_id = current_user_id.get()
            if not auth_user_id:
                raise ErrorException(errors.auth.UNAUTHORIZED, status_code=401)
            member_ids = await self._crm_read.member_ids_for_user(
                id_community=cid, auth_user_id=auth_user_id
            )
            if invoice.id_member not in member_ids:
                raise ErrorException(errors.auth.FORBIDDEN, status_code=403)

        if not invoice.artifact_uri:
            raise ErrorException(errors.billing.INVOICE_PDF_NOT_READY, status_code=422)

        try:
            bucket, key = storage.parse_s3_uri(invoice.artifact_uri)
        except ValueError as exc:
            logger.error("invoice %s malformed artifact_uri %r", invoice_id, invoice.artifact_uri)
            raise ErrorException(errors.billing.INVOICE_PDF_NOT_READY, status_code=422) from exc
        if bucket != self._settings.OUTPUT_BUCKET:
            logger.error("invoice %s artifact in unexpected bucket %s", invoice_id, bucket)
            raise ErrorException(errors.billing.INVOICE_PDF_NOT_READY, status_code=422)

        try:
            content = await storage.download_output(key)
        except storage.ObjectNotFound as exc:
            raise ErrorException(errors.billing.INVOICE_PDF_NOT_READY, status_code=422) from exc
        except storage.TransientStorageError as exc:
            raise ErrorException(errors.billing.GET_INVOICES, status_code=503) from exc

        return content, self._pdf_filename(invoice)

    async def delete_invoice_pdf(self, *, invoice_id: int) -> None:
        """Remove the rendered PDF (file + reference). Blocked once the invoice is sent.

        The invoice/legal record itself is untouched — only the generated document
        is deleted, and it can be regenerated afterwards.
        """
        cid = self._community()
        invoice = await self._repo.get_invoice(invoice_id)
        if invoice is None:
            raise ErrorException(errors.billing.INVOICE_NOT_FOUND, status_code=404)
        if not invoice.artifact_uri:
            return  # idempotent: nothing to delete
        if invoice.status in (
            InvoiceStatus.SENT,
            InvoiceStatus.PAID,
            InvoiceStatus.OVERDUE,
        ):
            raise ErrorException(errors.billing.INVOICE_PDF_DELETE_FORBIDDEN, status_code=409)

        try:
            bucket, key = storage.parse_s3_uri(invoice.artifact_uri)
            if bucket == self._settings.OUTPUT_BUCKET:
                await storage.delete_output(key)
        except ValueError:
            logger.warning("invoice %s malformed artifact_uri; clearing reference only", invoice_id)

        await self._repo.clear_artifact(invoice_id)
        await self._local.commit()

        await self._audit.log(
            AuditLogInput(
                action=AuditActions.INVOICE_PDF_DELETED,
                entity_type="invoice",
                entity_id=str(invoice_id),
                payload={},
            ),
            id_community=cid,
        )
        await self._crm.commit()

    # ---- read models -------------------------------------------------------
    async def get_run(self, *, run_id: int) -> BillingRunOut:
        run = await self._repo.get_run(run_id)
        if run is None:
            raise ErrorException(errors.billing.BILLING_RUN_NOT_FOUND, status_code=404)
        count = await self._repo.count_invoices_for_run(run_id)
        return mappers.run_to_out(run, invoice_count=count)

    async def list_runs(self, *, id_sharing_operation: int) -> list[BillingRunOut]:
        runs = await self._repo.list_runs(id_sharing_operation=id_sharing_operation)
        return [mappers.run_to_out(run) for run in runs]

    async def get_run_invoices(self, *, run_id: int) -> list[InvoiceOut]:
        run = await self._repo.get_run(run_id)
        if run is None:
            raise ErrorException(errors.billing.BILLING_RUN_NOT_FOUND, status_code=404)
        invoices = await self._repo.list_invoices_for_run(run_id)
        return [mappers.invoice_to_out(inv) for inv in invoices]

    async def get_invoice(self, *, invoice_id: int) -> InvoiceOut:
        invoice = await self._repo.get_invoice(invoice_id)
        if invoice is None:
            raise ErrorException(errors.billing.INVOICE_NOT_FOUND, status_code=404)
        lines = await self._repo.get_invoice_lines(invoice_id)
        return mappers.invoice_to_out(invoice, lines=lines)

    async def list_invoices(
        self,
        *,
        status: int | None,
        id_member: int | None,
        limit: int,
        offset: int,
        issued_from: datetime.date | None = None,
        issued_to: datetime.date | None = None,
        sort: str | None = None,
        order: str | None = None,
    ) -> tuple[list[InvoiceOut], int]:
        invoices, total = await self._repo.list_invoices(
            status=status,
            id_member=id_member,
            limit=limit,
            offset=offset,
            issued_from=issued_from,
            issued_to=issued_to,
            sort=sort,
            order=order,
        )
        return [mappers.invoice_to_out(inv) for inv in invoices], total

    async def list_my_invoices(
        self,
        *,
        status: int | None,
        limit: int,
        offset: int,
        issued_from: datetime.date | None = None,
        issued_to: datetime.date | None = None,
        sort: str | None = None,
        order: str | None = None,
    ) -> tuple[list[InvoiceOut], int]:
        """Caller-scoped: only the invoices of the authenticated user's own member(s).

        The member(s) are resolved from the auth user id (the ``x-user-id`` sub in
        ``current_user_id``) via the CRM, so a member cannot see anyone else's bills.
        """
        cid = self._community()
        auth_user_id = current_user_id.get()
        if not auth_user_id:
            raise ErrorException(errors.auth.UNAUTHORIZED, status_code=401)
        member_ids = await self._crm_read.member_ids_for_user(
            id_community=cid, auth_user_id=auth_user_id
        )
        if not member_ids:
            return [], 0
        invoices, total = await self._repo.list_invoices_for_members(
            id_members=member_ids,
            status=status,
            limit=limit,
            offset=offset,
            issued_from=issued_from,
            issued_to=issued_to,
            sort=sort,
            order=order,
        )
        return [mappers.invoice_to_out(inv) for inv in invoices], total
