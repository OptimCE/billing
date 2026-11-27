"""The billing part of a data export (monorepo contracts/export/README.md, §7).

User scope — the requester's own bills, as the app gives them:
- ``my-invoices.json``: ``/invoices/mine`` itself (``BillingService.list_my_invoices``), every page,
  in every community where a member is linked to them, run under the context variables the
  gateway would set; each invoice as ``GET /invoices/{id}`` shows it (with its lines) and its
  payments (``GET /invoices/{id}/payments``); credit notes and producer statements are documents
  of the same list.
- the PDF of each, as ``GET /invoices/{id}/pdf`` hands it to its owner.

Community scope (an ADMIN, re-checked in crm_db by the kit): every row this annex stores for the
community, table by table (contract §6: tariffs, runs, settlement snapshots, invoices, lines,
credit notes, payments), codes by name, amounts as written, and every invoice PDF.

The module's subscription is not checked: an export is not a use of the module. A PDF whose
object is gone, or that lives outside the documents bucket, stays described with
``file_missing: true`` and the ``files_missing`` counter instead of failing every export.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from decimal import Decimal
from enum import IntEnum
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.billing.repository import BillingRepository
from api.billing.service import BillingService
from core.audit_log import AuditLogService
from core.config import settings
from core.context_vars import (
    current_internal_community_id,
    current_user_id,
    current_user_role,
)
from core.export import (
    ExportHandler,
    ExportStorageSettings,
    PartContext,
    Requester,
    S3PartStore,
    SqlCrmLookup,
    resolve_requester,
    safe_file_name,
)
from core.storage.client import parse_s3_uri
from ports.crm_core_sqlalchemy import SqlAlchemyCrmCoreRead
from ports.providers import get_email_port, get_event_publisher
from regime.registry import get_registry
from shared.const import (
    BillingDirection,
    BillingRunStatus,
    InvoiceStatus,
    InvoiceType,
    Measure,
    PaymentMethod,
    TariffKind,
    TariffScope,
)
from shared.models.local_models import (
    BillingRunModel,
    CreditNoteModel,
    InvoiceLineModel,
    InvoiceModel,
    PaymentModel,
    SettlementSnapshotModel,
    TariffModel,
)

logger = logging.getLogger(__name__)

SERVICE = "billing"
PAGE = 500
# /invoices/mine caps a page at 200: the export reads them all, page by page.
MINE_PAGE = 200

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
# The size of an object (bucket, key), None when there is none.
SizeOf = Callable[[str, str], Awaitable[int | None]]


def _invoice_path(row: Any) -> str:
    name = row.number or "proforma"
    return f"files/invoices/{row.id}-{safe_file_name(name)}.pdf"


@dataclass(frozen=True)
class FileSpec:
    """A stored file a row points to: its ``s3://`` attribute and its place in the archive."""

    attribute: str
    path: Callable[[Any], str]
    content_type: str | None = None


@dataclass(frozen=True)
class Table:
    """One exported table: its rows, minus ``id_community`` (the scope), codes by name."""

    entity: str
    model: type[Any]
    # ORM attribute names, in order; written under the same name unless renamed.
    columns: tuple[str, ...]
    enums: Mapping[str, type[IntEnum]] = field(default_factory=dict)
    file: FileSpec | None = None
    rename: Mapping[str, str] = field(default_factory=dict)
    key: str = "id"


TABLES: tuple[Table, ...] = (
    Table(
        "tariffs",
        TariffModel,
        (
            "id",
            "id_sharing_operation",
            "kind",
            "scope",
            "scope_segment",
            "scope_ean",
            "price_per_kwh",
            "currency",
            "valid_from",
            "valid_to",
            "label",
            "created_at",
            "updated_at",
        ),
        {"kind": TariffKind, "scope": TariffScope},
    ),
    Table(
        "billing-runs",
        BillingRunModel,
        (
            "id",
            "id_sharing_operation",
            "period_start",
            "period_end",
            "status",
            "regulator",
            "kwh_scale",
            "warnings",
            "error_message",
            "created_at",
            "updated_at",
        ),
        {"status": BillingRunStatus},
    ),
    Table(
        "settlement-snapshots",
        SettlementSnapshotModel,
        (
            "id",
            "id_billing_run",
            "ean",
            "direction",
            "client_type",
            "shared_kwh",
            "inj_shared_kwh",
            "row_count",
            "distinct_ts_count",
            "id_member",
            "owned_from",
            "owned_to",
            "created_at",
        ),
        {"direction": BillingDirection},
    ),
    Table(
        "invoices",
        InvoiceModel,
        (
            "id",
            "id_billing_run",
            "id_member",
            "doc_type",
            "status",
            "number",
            "legal_entity_key",
            "currency",
            "subtotal",
            "vat_rate",
            "vat_amount",
            "total",
            "structured_comm",
            "issued_at",
            "due_date",
            "sent_at",
            "paid_at",
            "corrects_invoice_id",
            "artifact_sha256",
            "docgen_request_id",
            "created_at",
            "updated_at",
        ),
        {"doc_type": InvoiceType, "status": InvoiceStatus},
        file=FileSpec("artifact_uri", _invoice_path, "application/pdf"),
        rename={"doc_type": "type"},
    ),
    Table(
        "invoice-lines",
        InvoiceLineModel,
        (
            "id",
            "id_invoice",
            "ean",
            "direction",
            "measure",
            "quantity_kwh",
            "unit_price",
            "amount",
            "description",
            "created_at",
        ),
        {"direction": BillingDirection, "measure": Measure},
    ),
    Table(
        "credit-notes",
        CreditNoteModel,
        ("id", "id_original_invoice", "id_credit_invoice", "reason", "created_at"),
    ),
    Table(
        "payments",
        PaymentModel,
        ("id", "id_invoice", "amount", "currency", "method", "reference", "paid_at", "created_at"),
        {"method": PaymentMethod},
    ),
)

# The counters index.html shows, per entity written.
COMMUNITY_COUNTERS = {
    "tariffs": "tariffs",
    "billing-runs": "billing_runs",
    "invoices": "invoices",
    "payments": "payments",
}


def code(value: Any, enum: type[IntEnum] | None) -> Any:
    """A stored value as the archive writes it: a code by name, an amount as written, UUID text."""
    if value is None:
        return None
    if enum is not None:
        try:
            return enum(int(value)).name
        except ValueError:
            # A code retired since keeps its number rather than failing the export.
            return int(value)
    if isinstance(value, Decimal):
        # Plain notation, every stored digit: never "0E-10".
        return format(value, "f")
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


class _Part:
    def __init__(
        self,
        ctx: PartContext,
        *,
        local: AsyncSession,
        crm: AsyncSession,
        size_of: SizeOf,
        bucket: str,
    ) -> None:
        self.ctx = ctx
        self.local = local
        self.crm = crm
        self.size_of = size_of
        self.bucket = bucket
        self.files = 0
        self.missing = 0

    async def file(self, uri: str | None, path: str) -> dict[str, Any] | None:
        """References an invoice PDF, or describes it as missing. None when there is none."""
        if not uri:
            return None
        self.ctx.checkpoint()
        try:
            bucket, key = parse_s3_uri(uri)
        except ValueError:
            bucket, key = "", ""
        # The API serves a PDF from the documents bucket only (download_invoice_pdf).
        size = await self.size_of(bucket, key) if bucket == self.bucket and key else None
        if size is None:
            self.missing += 1
            return {"path": None, "file_missing": True}
        self.ctx.add_reference(
            bucket=bucket, key=key, path=path, size=size, content_type="application/pdf"
        )
        self.files += 1
        return {"path": f"{SERVICE}/{path}", "file_missing": False}

    def finish(self) -> None:
        self.ctx.count("files", self.files)
        if self.missing:
            self.ctx.count("files_missing", self.missing)

    # ---- community scope ---------------------------------------------------------------------

    async def community_export(self, id_community: int) -> None:
        for table in TABLES:
            written = await self.ctx.write_json(table.entity, self._rows(table, id_community))
            if table.entity in COMMUNITY_COUNTERS:
                self.ctx.count(COMMUNITY_COUNTERS[table.entity], written)
        self.finish()

    async def _rows(self, table: Table, id_community: int) -> AsyncIterator[dict[str, Any]]:
        key = getattr(table.model, table.key)
        last: Any = None
        while True:
            self.ctx.checkpoint()
            query = select(table.model).where(table.model.id_community == id_community)
            if last is not None:
                query = query.where(key > last)
            rows = list((await self.local.execute(query.order_by(key).limit(PAGE))).scalars())
            for row in rows:
                item = {
                    table.rename.get(name, name): code(getattr(row, name), table.enums.get(name))
                    for name in table.columns
                }
                if table.file is not None:
                    item["file"] = await self.file(
                        getattr(row, table.file.attribute), table.file.path(row)
                    )
                yield item
            if len(rows) < PAGE:
                return
            last = getattr(rows[-1], table.key)

    # ---- user scope --------------------------------------------------------------------------

    async def user_export(self, requester: Requester) -> None:
        member_ids = list(requester.member_ids)
        communities: dict[int, str | None] = {}
        if member_ids:
            result = await self.crm.execute(
                text(
                    "SELECT DISTINCT m.id_community, c.name FROM member m "
                    "JOIN community c ON c.id = m.id_community "
                    "WHERE m.id = ANY(:ids) ORDER BY m.id_community"
                ),
                {"ids": member_ids},
            )
            communities = {int(row[0]): row[1] for row in result}

        invoices: list[dict[str, Any]] = []
        for id_community, name in communities.items():
            self.ctx.checkpoint()
            invoices.extend(await self._mine(requester.auth_user_id, id_community, name))
        self.ctx.count("invoices", await self.ctx.write_json("my-invoices", invoices))
        self.finish()

    async def _mine(
        self, auth_user_id: str, id_community: int, community: str | None
    ) -> list[dict[str, Any]]:
        """``/invoices/mine`` in one community, under the context variables the gateway sets
        (a member's: no role, so nothing a manager would see)."""
        tokens = (
            current_user_id.set(auth_user_id),
            current_internal_community_id.set(id_community),
            current_user_role.set(None),
        )
        try:
            repository = BillingRepository(self.local)
            service = BillingService(
                local_session=self.local,
                crm_session=self.crm,
                repository=repository,
                crm_read=SqlAlchemyCrmCoreRead(self.crm),
                registry=get_registry(),
                publisher=get_event_publisher(),
                email=get_email_port(),
                audit=AuditLogService(self.crm),
                settings=settings,
            )
            out: list[dict[str, Any]] = []
            offset = 0
            while True:
                page, total = await service.list_my_invoices(
                    status=None, limit=MINE_PAGE, offset=offset
                )
                for listed in page:
                    self.ctx.checkpoint()
                    invoice = await service.get_invoice(invoice_id=listed.id)
                    payments = await service.list_payments(invoice_id=listed.id)
                    row = await repository.get_invoice(listed.id)
                    out.append(
                        {
                            "id_community": id_community,
                            "community": community,
                            **invoice.model_dump(mode="json"),
                            "payments": [p.model_dump(mode="json") for p in payments],
                            "file": await self.file(
                                row.artifact_uri if row else None, _invoice_path(listed)
                            ),
                        }
                    )
                offset += MINE_PAGE
                if not page or offset >= total:
                    return out
        finally:
            current_user_role.reset(tokens[2])
            current_internal_community_id.reset(tokens[1])
            current_user_id.reset(tokens[0])


# ---- wiring -----------------------------------------------------------------------------------


def export_handler(
    *,
    local: SessionFactory,
    crm: SessionFactory,
    size_of: SizeOf,
    bucket: str,
) -> ExportHandler:
    """The handler the kit calls for each part of this service."""

    async def handle(ctx: PartContext) -> None:
        async with crm() as crm_session, local() as local_session:
            requester = await resolve_requester(SqlCrmLookup(crm_session), ctx.request)
            part = _Part(ctx, local=local_session, crm=crm_session, size_of=size_of, bucket=bucket)
            if requester.community_id is None:
                await part.user_export(requester)
            else:
                await part.community_export(requester.community_id)

    return handle


async def _s3_size(bucket: str, key: str) -> int | None:
    from core.storage import size

    return await size(key, bucket=bucket)


def start_export_consumer(stop: asyncio.Event) -> asyncio.Task[None] | None:
    """Starts this service's export consumer, or logs why it cannot (and the API runs on).

    Needs the export account (EXPORT_STORAGE_*), never the service's usual storage account.
    """
    from core.database.database import AsyncSessionCRMFactory, AsyncSessionLocalFactory
    from core.export.consumer import run_export_consumer
    from core.queue.init import get_jetstream

    try:
        storage = ExportStorageSettings.from_env()
        js = get_jetstream()
    except RuntimeError as err:
        logger.warning(
            "Data export disabled: %s",
            err,
            extra={"operation": "export:consumer_disabled", "service": SERVICE},
        )
        return None
    handler = export_handler(
        local=AsyncSessionLocalFactory,
        crm=AsyncSessionCRMFactory,
        size_of=_s3_size,
        bucket=settings.OUTPUT_BUCKET,
    )
    return asyncio.create_task(
        run_export_consumer(
            js,
            service=SERVICE,
            handler=handler,
            store=S3PartStore(storage),
            stop=stop,
            expected_bucket=storage.bucket,
        ),
        name="billing-export-consumer",
    )


async def stop_export_consumer(
    task: asyncio.Task[None] | None, stop: asyncio.Event, grace: float = 10.0
) -> None:
    """Stops pulling; a part in progress is aborted and nak'ed (redelivered later)."""
    stop.set()
    if task is None:
        return
    try:
        await asyncio.wait_for(task, timeout=grace)
    except TimeoutError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
