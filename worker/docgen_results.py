"""Consume ``docgen.result.billing`` and attach the rendered PDF to its invoice.

Correlates by ``request_id`` (stored on the invoice at issue). The docgen result
stream is owned by the document-generation service; billing only adds a durable
consumer. ``process_docgen_result`` is extracted so it can be driven in tests.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from nats.aio.msg import Msg
from nats.js import JetStreamContext
from nats.js.api import ConsumerConfig
from sqlalchemy.ext.asyncio import AsyncSession

from api.billing.repository import BillingRepository
from core import metrics as app_metrics
from core.audit_log import AuditActions, AuditLogInput, AuditLogService
from core.config import settings
from core.database.database import AsyncSessionCRMFactory, AsyncSessionLocalFactory
from core.queue.helper import subscribe_durable
from shared.const import InvoiceStatus
from worker.context import with_tenant

logger = logging.getLogger(__name__)

_DOCGEN_DURABLE = "worker-billing-docgen"
_ACK_WAIT_SECONDS = 60
_NAK_RETRY_DELAY_SECONDS = 30
# A result that stays transient (a non-permanent docgen failure, a success
# without a PDF, a crash while handling it) is retried up to this many
# deliveries, then parked on the DLQ. Unbounded, a result that failed every time
# redelivered every 30 s forever.
_MAX_DELIVER = 5


def _tenant_id_from(body: dict) -> str | int | None:
    """Read the tenant from a docgen result.

    ``GenerationResult`` is ``extra="forbid"`` and has NO ``tenant_id`` field —
    it exists only on the *request*. The one channel that round-trips is
    ``metadata``, which document-generation echoes verbatim, so that is where the
    tenant actually arrives. The top-level read is kept first only so a hand-built
    payload (older tests, a manual replay) still works.
    """
    top_level = body.get("tenant_id")
    if top_level is not None:
        return top_level  # type: ignore[no-any-return]  # untyped JSON body
    metadata = body.get("metadata")
    if not isinstance(metadata, dict):
        return None
    return metadata.get("tenant_id")


async def process_docgen_result(
    body: dict,
    *,
    local_session: AsyncSession | None = None,
    crm_session: AsyncSession | None = None,
) -> str:
    """Attach/mark from a docgen result. Returns one of:
    ``attached`` | ``render_failed`` | ``transient`` | ``drop``.
    """
    own_local = local_session is None
    own_crm = crm_session is None
    local = local_session or AsyncSessionLocalFactory()
    crm = crm_session or AsyncSessionCRMFactory()
    try:
        request_id = body.get("request_id")
        tenant_id = _tenant_id_from(body)
        if not request_id or tenant_id is None:
            logger.error("docgen result missing request_id/tenant_id: %r", body)
            return "drop"
        cid = int(tenant_id)

        repo = BillingRepository(local)
        with with_tenant(cid):
            invoice = await repo.find_invoice_by_docgen_request(request_id)
            if invoice is None:
                logger.warning("docgen result for unknown request_id %s", request_id)
                return "drop"
            if invoice.artifact_uri:
                return "attached"  # idempotent redelivery

            if body.get("status") == "success":
                artifacts = body.get("artifacts") or []
                pdf = next((a for a in artifacts if a.get("format") == "pdf"), None)
                if pdf is None or not pdf.get("uri"):
                    logger.warning("docgen success without a pdf artifact for %s", request_id)
                    return "transient"
                # A successful re-render clears a prior RENDER_FAILED back to ISSUED.
                recovered = invoice.status == InvoiceStatus.RENDER_FAILED
                await repo.attach_artifact(
                    invoice.id,
                    uri=pdf["uri"],
                    sha256=pdf.get("sha256"),
                    set_status=InvoiceStatus.ISSUED if recovered else None,
                )
                if own_local:
                    await local.commit()
                await AuditLogService(crm).log(
                    AuditLogInput(
                        action=AuditActions.INVOICE_RENDERED,
                        entity_type="invoice",
                        entity_id=str(invoice.id),
                        payload={"uri": pdf["uri"]},
                    ),
                    id_community=cid,
                )
                if own_crm:
                    await crm.commit()
                app_metrics.invoices_rendered.add(1, {"outcome": "attached"})
                return "attached"

            error = body.get("error") or {}
            if error.get("permanent"):
                # A DRAFT proforma render failing must NOT push the invoice into
                # RENDER_FAILED (that status is for issued invoices); just log + ack.
                if invoice.status == InvoiceStatus.DRAFT:
                    logger.warning(
                        "proforma render failed permanently for invoice %s: %s",
                        invoice.id,
                        error.get("code"),
                    )
                    app_metrics.invoices_rendered.add(1, {"outcome": "failed"})
                    return "render_failed"
                await repo.mark_render_failed(invoice.id)
                if own_local:
                    await local.commit()
                await AuditLogService(crm).log(
                    AuditLogInput(
                        action=AuditActions.INVOICE_RENDER_FAILED,
                        entity_type="invoice",
                        entity_id=str(invoice.id),
                        payload={"error": error.get("code")},
                    ),
                    id_community=cid,
                )
                if own_crm:
                    await crm.commit()
                app_metrics.invoices_rendered.add(1, {"outcome": "failed"})
                return "render_failed"

            return "transient"
    finally:
        if own_local:
            await local.close()
        if own_crm:
            await crm.close()


async def subscribe(js: JetStreamContext, *, on_dlq: Callable[[Msg], Awaitable[None]]):
    """Subscribe to the docgen result subject (stream owned by document-generation).

    A result still transient on its ``_MAX_DELIVER``-th delivery goes to
    ``on_dlq`` and is acked. Nothing else changes: the invoice keeps its status
    and stays without a PDF, which can be requested again. The broker's own
    ``max_deliver`` is one higher, as a backstop for deliveries the handler never
    settles (a worker killed mid-message). The spare delivery lets the handler
    still park a result whose last regular delivery was lost that way.
    """

    async def _handle(msg: Msg) -> None:
        try:
            body = json.loads(msg.data)
        except Exception:
            logger.exception("Undecodable docgen result; acking and dropping")
            await msg.ack()
            return
        try:
            outcome = await process_docgen_result(body)
        except Exception:
            logger.exception("docgen result handler crashed")
            outcome = "transient"

        if outcome != "transient":
            await msg.ack()
            return

        delivered = msg.metadata.num_delivered
        if delivered >= _MAX_DELIVER:
            logger.error(
                "docgen result %s still transient after %d deliveries; routing to DLQ",
                body.get("request_id") if isinstance(body, dict) else None,
                delivered,
            )
            await on_dlq(msg)
            await msg.ack()
        else:
            await msg.nak(delay=_NAK_RETRY_DELAY_SECONDS)

    return await subscribe_durable(
        js,
        subject=settings.DOCGEN_RESULT_SUBJECT,
        durable=_DOCGEN_DURABLE,
        cb=_handle,
        config=ConsumerConfig(ack_wait=_ACK_WAIT_SECONDS, max_deliver=_MAX_DELIVER + 1),
    )
