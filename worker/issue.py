"""Worker-side issue processing: build the docgen payload and request a render.

Phase 2 of the two-phase issue: the invoice is already legally numbered/ISSUED
(the API did that); here we ask document-generation for the PDF. The artifact is
attached later by the docgen-results handler. Idempotent: a redelivery reuses the
stored ``docgen_request_id`` (same render key) and no-ops once the artifact is set.
"""

from __future__ import annotations

import datetime
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from api.billing.mappers import build_docgen_data
from api.billing.repository import BillingRepository
from core.config import settings
from core.database.database import AsyncSessionCRMFactory, AsyncSessionLocalFactory
from ports.crm_core_sqlalchemy import SqlAlchemyCrmCoreRead
from ports.document_generation import DocgenRequest, DocumentGenerationPort
from regime.registry import RegimeConfigError, get_registry
from shared.const import InvoiceStatus, InvoiceType
from worker.context import with_tenant

logger = logging.getLogger(__name__)


class IssueRenderError(Exception):
    """A non-retryable issue-render failure (missing community/regime)."""


async def process_issue(
    invoice_id: int,
    *,
    doc_port: DocumentGenerationPort,
    local_session: AsyncSession | None = None,
    crm_session: AsyncSession | None = None,
) -> bool:
    """Request a PDF render for an issued invoice. Returns False if not found."""
    own_local = local_session is None
    own_crm = crm_session is None
    local = local_session or AsyncSessionLocalFactory()
    crm = crm_session or AsyncSessionCRMFactory()
    try:
        repo = BillingRepository(local)
        crm_read = SqlAlchemyCrmCoreRead(crm)
        invoice = await repo.get_invoice_by_id(invoice_id)
        if invoice is None:
            logger.warning("process_issue: invoice %s not found", invoice_id)
            return False

        with with_tenant(invoice.id_community):
            if invoice.artifact_uri:
                return True  # already rendered

            identity = await crm_read.get_community_identity(id_community=invoice.id_community)
            if identity is None:
                raise IssueRenderError(f"community {invoice.id_community} not found")
            try:
                regime = get_registry().get_for(identity.regulator)
            except RegimeConfigError as exc:
                raise IssueRenderError(f"no regime for regulator {identity.regulator}") from exc

            lines = await repo.get_invoice_lines(invoice_id)
            contacts = await crm_read.participant_contacts(
                id_community=invoice.id_community, member_ids=[invoice.id_member]
            )
            contact = contacts.get(invoice.id_member)

            request_id = invoice.docgen_request_id
            if request_id is None:
                request_id = uuid.uuid4().hex
                await repo.set_docgen_request_id(invoice_id, request_id)
                if own_local:
                    await local.commit()  # persist BEFORE publish so redelivery reuses it

            # A DRAFT (unissued) consumer invoice renders as a watermarked
            # proforma with no legal number; issuing later clears the artifact so
            # the numbered PDF is regenerated from the real template.
            is_draft = invoice.status == InvoiceStatus.DRAFT
            if invoice.doc_type == InvoiceType.PRODUCER_STATEMENT:
                template_uri = settings.PRODUCER_STATEMENT_TEMPLATE_URI
            elif is_draft:
                template_uri = settings.INVOICE_PROFORMA_TEMPLATE_URI
            else:
                template_uri = settings.INVOICE_TEMPLATE_URI
            data = build_docgen_data(
                invoice=invoice,
                lines=lines,
                identity=identity,
                contact=contact,
                regime=regime,
                locale=settings.DEFAULT_LOCALE,
            )
            issued = invoice.issued_at or datetime.datetime.now(datetime.UTC)
            folder = "proforma" if is_draft else "invoices"
            key_prefix = f"billing/{folder}/{issued:%Y/%m}/inv-{invoice_id}/"
            await doc_port.request_render(
                DocgenRequest(
                    request_id=request_id,
                    tenant_id=str(invoice.id_community),
                    template_uri=template_uri,
                    data=data,
                    key_prefix=key_prefix,
                    reply_to=settings.DOCGEN_RESULT_SUBJECT,
                    locale=settings.DEFAULT_LOCALE,
                    presign_ttl=settings.DOCGEN_PRESIGN_TTL,
                    metadata={"invoice_id": invoice_id},
                )
            )
        return True
    finally:
        if own_local:
            await local.close()
        if own_crm:
            await crm.close()
