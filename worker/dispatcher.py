"""NATS subscriptions + message handlers for the billing worker.

Failure-class matrix (mirrors the template):

* Deterministic (no regime, no tariff, bad payload) → mark the run FAILED and
  ack. Never redeliver a poison pill.
* Transient (DB hiccup) → nak for redelivery, until ``max_deliver`` is reached,
  then route to the DLQ + mark FAILED + ack.

``subscribe_all`` is the single wiring point the entrypoint calls; it returns the
subscriptions to drain on shutdown. The issue/docgen consumers land in Phase 5.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from nats.aio.msg import Msg
from nats.js import JetStreamContext
from nats.js.api import ConsumerConfig

from core.queue.helper import Event, send_event
from ports.document_generation_nats import NatsDocumentGeneration
from shared.const import (
    SUBJECT_DLQ_ISSUE,
    SUBJECT_DLQ_RUN,
    SUBJECT_INVOICE_ISSUE_REQUESTED,
    SUBJECT_RUN_COMPLETED,
    SUBJECT_RUN_REQUESTED,
)
from worker import docgen_results, issue, persistence

logger = logging.getLogger(__name__)

_RUN_DURABLE = "worker-billing-run"
_ISSUE_DURABLE = "worker-billing-issue"
_ACK_WAIT_SECONDS = 5 * 60
_ISSUE_ACK_WAIT_SECONDS = 60
_NAK_RETRY_DELAY_SECONDS = 30
_MAX_DELIVER = 5

# The docgen result stream is owned by document-generation. On a cold stack that
# service may create the stream *after* we boot, so the first subscribe attempt
# can lose the race. Rather than leave PDF-attach disabled for the whole process
# lifetime, retry in the background with capped backoff (~20 min window).
_DOCGEN_RETRY_BASE_DELAY_SECONDS = 1
_DOCGEN_RETRY_MAX_DELAY_SECONDS = 30
_DOCGEN_RETRY_MAX_ATTEMPTS = 40


async def subscribe_all(
    js: JetStreamContext, *, inflight: set[asyncio.Task] | None = None
) -> list:
    """Register the billing durable consumers. Returns their subscriptions."""
    subs = [
        await js.subscribe(
            subject=SUBJECT_RUN_REQUESTED,
            durable=_RUN_DURABLE,
            queue=_RUN_DURABLE,
            manual_ack=True,
            cb=_make_handler(js, _process_run_message, inflight=inflight),
            config=ConsumerConfig(ack_wait=_ACK_WAIT_SECONDS, max_deliver=_MAX_DELIVER),
        ),
        await js.subscribe(
            subject=SUBJECT_INVOICE_ISSUE_REQUESTED,
            durable=_ISSUE_DURABLE,
            queue=_ISSUE_DURABLE,
            manual_ack=True,
            cb=_make_handler(js, _process_issue_message, inflight=inflight),
            config=ConsumerConfig(ack_wait=_ISSUE_ACK_WAIT_SECONDS, max_deliver=_MAX_DELIVER),
        ),
    ]
    logger.info("Subscribed billing run + issue consumers")
    # The docgen result stream is owned by document-generation. Try once; if it
    # isn't there yet (cold-start race), retry in the background so PDF-attach
    # self-heals instead of staying disabled for the process lifetime.
    try:
        subs.append(await docgen_results.subscribe(js))
        logger.info("Subscribed docgen results consumer")
    except Exception as exc:
        logger.warning(
            "docgen results not ready (%s); retrying attach in the background", exc
        )
        _spawn_docgen_result_retry(js, inflight=inflight)
    return subs


def _spawn_docgen_result_retry(
    js: JetStreamContext, *, inflight: set[asyncio.Task] | None
) -> None:
    """Background-retry the docgen-results subscription until it succeeds.

    The subscription is not appended to the caller's drain list — mutating that
    list from here would race the shutdown drain; the late subscription is torn
    down by the connection-level drain in ``close_nats`` instead. The task is
    tracked in ``inflight`` so shutdown cancels it cleanly. In the inline unit
    path (``inflight is None``) there is no loop machinery, so we skip the retry.
    """
    if inflight is None:
        return

    async def _retry() -> None:
        for attempt in range(1, _DOCGEN_RETRY_MAX_ATTEMPTS + 1):
            delay = min(
                _DOCGEN_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)),
                _DOCGEN_RETRY_MAX_DELAY_SECONDS,
            )
            await asyncio.sleep(delay)
            try:
                await docgen_results.subscribe(js)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("docgen results attach retry %d failed: %s", attempt, exc)
                continue
            logger.info("docgen results attached after %d background retr(y/ies)", attempt)
            return
        logger.warning(
            "docgen results still unavailable after %d retries; PDF attach disabled",
            _DOCGEN_RETRY_MAX_ATTEMPTS,
        )

    task: asyncio.Task[None] = asyncio.create_task(_retry(), name="docgen-result-retry")
    inflight.add(task)
    task.add_done_callback(inflight.discard)


def _make_handler(
    js: JetStreamContext,
    process: Callable[[JetStreamContext, Msg], Coroutine[Any, Any, None]],
    *,
    inflight: set[asyncio.Task] | None,
) -> Callable[[Msg], Awaitable[None]]:
    """Build the per-message callback.

    With ``inflight`` set, each message is handled as a tracked background task so
    the listener never blocks; without it (unit tests) the message is handled
    inline to completion.
    """

    def _on_done(task: asyncio.Task) -> None:
        if inflight is not None:
            inflight.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("billing handler task crashed: %r", task.exception())

    async def handle(msg: Msg) -> None:
        if inflight is None:
            await process(js, msg)
            return
        task: asyncio.Task[None] = asyncio.create_task(process(js, msg))
        inflight.add(task)
        task.add_done_callback(_on_done)

    return handle


async def _process_run_message(js: JetStreamContext, msg: Msg) -> None:
    try:
        event = Event.decode(msg.data)
    except Exception:
        logger.exception("Undecodable message on %s; acking and dropping", SUBJECT_RUN_REQUESTED)
        await msg.ack()
        return

    run_id = event.data.get("billing_run_id") if isinstance(event.data, dict) else None
    if not isinstance(run_id, int):
        logger.error("Missing/invalid billing_run_id on %s: %r", SUBJECT_RUN_REQUESTED, event.data)
        await msg.ack()
        return

    try:
        count = await persistence.process_billing_run(run_id)
    except persistence.DeterministicRunError as exc:
        logger.warning("Billing run %s deterministic failure: %s", run_id, exc)
        await persistence.mark_run_failed(run_id, str(exc))
        await msg.ack()
        return
    except Exception as exc:
        delivered = msg.metadata.num_delivered
        if delivered >= _MAX_DELIVER:
            logger.error("Billing run %s exhausted retries; routing to DLQ", run_id)
            await _to_dlq(js, SUBJECT_DLQ_RUN, msg)
            await persistence.mark_run_failed(run_id, f"exhausted retries: {exc}")
            await msg.ack()
        else:
            logger.warning("Billing run %s transient failure; will redeliver: %s", run_id, exc)
            await msg.nak(delay=_NAK_RETRY_DELAY_SECONDS)
        return

    try:
        await send_event(
            js,
            SUBJECT_RUN_COMPLETED,
            Event(
                type="billing.run.completed",
                data={"billing_run_id": run_id, "invoice_count": count},
            ),
        )
    except Exception:
        logger.exception("Failed to publish run.completed for %s", run_id)

    await msg.ack()


async def _process_issue_message(js: JetStreamContext, msg: Msg) -> None:
    try:
        event = Event.decode(msg.data)
    except Exception:
        logger.exception(
            "Undecodable message on %s; acking and dropping", SUBJECT_INVOICE_ISSUE_REQUESTED
        )
        await msg.ack()
        return

    invoice_id = event.data.get("invoice_id") if isinstance(event.data, dict) else None
    if not isinstance(invoice_id, int):
        logger.error(
            "Missing/invalid invoice_id on %s: %r", SUBJECT_INVOICE_ISSUE_REQUESTED, event.data
        )
        await msg.ack()
        return

    try:
        await issue.process_issue(invoice_id, doc_port=NatsDocumentGeneration(js))
    except issue.IssueRenderError as exc:
        logger.warning("Invoice %s render request deterministic failure: %s", invoice_id, exc)
        await msg.ack()
        return
    except Exception as exc:
        delivered = msg.metadata.num_delivered
        if delivered >= _MAX_DELIVER:
            logger.error("Invoice %s issue exhausted retries; routing to DLQ", invoice_id)
            await _to_dlq(js, SUBJECT_DLQ_ISSUE, msg)
            await msg.ack()
        else:
            logger.warning(
                "Invoice %s issue transient failure; will redeliver: %s", invoice_id, exc
            )
            await msg.nak(delay=_NAK_RETRY_DELAY_SECONDS)
        return

    await msg.ack()


async def _to_dlq(js: JetStreamContext, subject: str, msg: Msg) -> None:
    """Best-effort park of a poison message on the DLQ for inspection."""
    try:
        await js.publish(subject, msg.data, headers={"dlq-origin": msg.subject})
    except Exception:
        logger.exception("Failed to route message to DLQ %s", subject)
