"""How the docgen-result consumer settles a result it cannot use yet.

``process_docgen_result`` answers "transient" for a result that a later delivery
might still turn into a PDF: a non-permanent docgen failure, or a success
without a PDF artifact. A crash while handling a result counts the same. Such a
result is nak'd until its ``_MAX_DELIVER``-th delivery, then published to the
billing DLQ and acked. It used to be nak'd every 30 s forever.

The handler under test is the one ``dispatcher.subscribe_all`` registers, so the
DLQ subject it publishes to comes from the dispatcher's own wiring.
"""

from __future__ import annotations

import asyncio
import functools
import json
from types import SimpleNamespace

import pytest

from core.config import settings
from shared.const import (
    SUBJECT_DLQ_DOCGEN_RESULT,
    SUBJECT_INVOICE_ISSUE_REQUESTED,
    SUBJECT_RUN_REQUESTED,
    InvoiceStatus,
)
from shared.models.local_models import InvoiceModel
from tests.billing.test_invoice_pdf import _issue, _seed_single, _set, _wire_result
from tests.fake_jetstream import FakeJetStream
from worker import dispatcher, docgen_results

_LAST = docgen_results._MAX_DELIVER
_RETRY = ("nak", docgen_results._NAK_RETRY_DELAY_SECONDS)
_BODY = json.dumps({"request_id": "req-1", "status": "failed"}).encode()


class FakeMsg:
    """``nats.aio.msg.Msg`` stand-in that records how the handler settled it."""

    def __init__(self, events: list[tuple], *, num_delivered: int, data: bytes = _BODY) -> None:
        self.data = data
        self.subject = settings.DOCGEN_RESULT_SUBJECT
        self.metadata = SimpleNamespace(num_delivered=num_delivered)
        self._events = events

    async def ack(self) -> None:
        self._events.append(("ack",))

    async def nak(self, delay: float | None = None) -> None:
        self._events.append(("nak", delay))


class _PublishingJetStream(FakeJetStream):
    """Also records ``publish``, which ``dispatcher._to_dlq`` parks a message with.

    It logs into the same list as ``FakeMsg``, so a test sees whether the DLQ
    publish came before the ack.
    """

    def __init__(self, streams: dict[str, list[str]], events: list[tuple]) -> None:
        super().__init__(streams)
        self._events = events

    async def publish(self, subject: str, payload: bytes, headers: dict | None = None, **_):
        self._events.append(("publish", subject, payload, headers))


def _streams(*, with_results: bool = True) -> dict[str, list[str]]:
    streams = {"BILLING": [SUBJECT_RUN_REQUESTED, SUBJECT_INVOICE_ISSUE_REQUESTED]}
    if with_results:
        streams["DOCGEN_RESULTS"] = ["docgen.result.>"]
    return streams


def _result_handler(js: FakeJetStream):
    durable = docgen_results._DOCGEN_DURABLE
    return next(s["cb"] for s in js.subscriptions if s["durable"] == durable)


async def _subscribed_handler(events: list[tuple]):
    js = _PublishingJetStream(_streams(), events)
    await dispatcher.subscribe_all(js)  # type: ignore[arg-type]  # duck-typed FakeJetStream
    return _result_handler(js)


def _parked(data: bytes = _BODY) -> list[tuple]:
    """Published to the DLQ, then acked: a crash between the two redelivers it."""
    origin = {"dlq-origin": settings.DOCGEN_RESULT_SUBJECT}
    return [("publish", SUBJECT_DLQ_DOCGEN_RESULT, data, origin), ("ack",)]


def _returning(outcome: str):
    async def process(body):
        return outcome

    return process


def _crashing():
    async def process(body):
        raise RuntimeError("database went away")

    return process


@pytest.mark.parametrize("delivery", range(1, _LAST))
async def test_transient_result_is_redelivered_before_its_last_delivery(monkeypatch, delivery):
    monkeypatch.setattr(docgen_results, "process_docgen_result", _returning("transient"))
    events: list[tuple] = []
    handle = await _subscribed_handler(events)

    await handle(FakeMsg(events, num_delivered=delivery))

    assert events == [_RETRY]


async def test_transient_result_on_its_last_delivery_is_parked_on_the_dlq(monkeypatch):
    monkeypatch.setattr(docgen_results, "process_docgen_result", _returning("transient"))
    events: list[tuple] = []
    handle = await _subscribed_handler(events)

    await handle(FakeMsg(events, num_delivered=_LAST))

    assert events == _parked()


async def test_the_brokers_spare_delivery_is_parked_too(monkeypatch):
    """The consumer allows one delivery past the bound, for a last regular
    delivery the handler never settled (a worker killed mid-message)."""
    monkeypatch.setattr(docgen_results, "process_docgen_result", _returning("transient"))
    events: list[tuple] = []
    handle = await _subscribed_handler(events)

    await handle(FakeMsg(events, num_delivered=_LAST + 1))

    assert events == _parked()


@pytest.mark.parametrize(("delivery", "expected"), [(_LAST - 1, [_RETRY]), (_LAST, _parked())])
async def test_a_crash_is_bounded_like_a_transient_result(monkeypatch, delivery, expected):
    monkeypatch.setattr(docgen_results, "process_docgen_result", _crashing())
    events: list[tuple] = []
    handle = await _subscribed_handler(events)

    await handle(FakeMsg(events, num_delivered=delivery))

    assert events == expected


async def test_a_body_that_is_not_an_object_still_reaches_the_dlq(monkeypatch):
    """Valid JSON, but not the object the handler reads ``request_id`` from."""
    monkeypatch.setattr(docgen_results, "process_docgen_result", _crashing())
    events: list[tuple] = []
    handle = await _subscribed_handler(events)

    await handle(FakeMsg(events, num_delivered=_LAST, data=b"[]"))

    assert events == _parked(b"[]")


@pytest.mark.parametrize("outcome", ["attached", "render_failed", "drop"])
@pytest.mark.parametrize("delivery", [1, _LAST])
async def test_a_settled_outcome_is_acked_without_the_dlq(monkeypatch, outcome, delivery):
    monkeypatch.setattr(docgen_results, "process_docgen_result", _returning(outcome))
    events: list[tuple] = []
    handle = await _subscribed_handler(events)

    await handle(FakeMsg(events, num_delivered=delivery))

    assert events == [("ack",)]


async def test_a_subscription_attached_after_boot_parks_on_the_same_dlq(monkeypatch):
    """Cold start: document-generation creates its results stream after the
    billing worker boots, so the background retry is what subscribes."""
    monkeypatch.setattr(dispatcher, "_DOCGEN_RETRY_BASE_DELAY_SECONDS", 0)
    monkeypatch.setattr(docgen_results, "process_docgen_result", _returning("transient"))
    events: list[tuple] = []
    streams = _streams(with_results=False)
    js = _PublishingJetStream(streams, events)
    inflight: set[asyncio.Task] = set()

    assert len(await dispatcher.subscribe_all(js, inflight=inflight)) == 2
    streams["DOCGEN_RESULTS"] = ["docgen.result.>"]
    await asyncio.gather(*inflight)

    stored = js.stored(settings.DOCGEN_RESULT_SUBJECT, docgen_results._DOCGEN_DURABLE)
    assert stored.max_deliver == _LAST + 1
    await _result_handler(js)(FakeMsg(events, num_delivered=_LAST))
    assert events == _parked()


async def test_a_result_that_never_succeeds_is_parked_and_the_invoice_left_alone(
    client, db_session, monkeypatch
):
    """End to end with the real ``process_docgen_result``: docgen gave up on the
    render without calling it permanent. Retried, parked, and nothing else: the
    invoice is not marked RENDER_FAILED, so its PDF can simply be requested again."""
    cid, _member, invoice = await _seed_single(client, db_session)
    invoice_id = invoice["id"]
    await _issue(client, invoice_id)
    await _set(db_session, invoice_id, docgen_request_id="flaky-req")
    data = json.dumps(
        _wire_result(
            "flaky-req",
            cid,
            status="failed",
            artifacts=[],
            error={"code": "RENDER_ERROR", "message": "unhandled_worker_error", "permanent": False},
        )
    ).encode()
    monkeypatch.setattr(
        docgen_results,
        "process_docgen_result",
        functools.partial(
            docgen_results.process_docgen_result, local_session=db_session, crm_session=db_session
        ),
    )
    events: list[tuple] = []
    handle = await _subscribed_handler(events)

    for delivery in range(1, _LAST + 1):
        await handle(FakeMsg(events, num_delivered=delivery, data=data))

    assert events == [_RETRY] * (_LAST - 1) + _parked(data)
    db_session.expire_all()
    fresh = await db_session.get(InvoiceModel, invoice_id)
    assert (fresh.status, fresh.artifact_uri, fresh.docgen_request_id) == (
        InvoiceStatus.ISSUED,
        None,
        "flaky-req",
    )
