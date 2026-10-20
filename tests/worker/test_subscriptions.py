"""The billing worker's durables take the settings the code declares.

Including durables an earlier build created with other settings: ``js.subscribe``
alone keeps those, and ``FakeJetStream`` reproduces that.
"""

from __future__ import annotations

from core.config import settings
from shared.const import SUBJECT_INVOICE_ISSUE_REQUESTED, SUBJECT_RUN_REQUESTED
from tests.fake_jetstream import FakeJetStream
from worker import dispatcher, docgen_results


def _js() -> FakeJetStream:
    return FakeJetStream(
        {
            "BILLING": [SUBJECT_RUN_REQUESTED, SUBJECT_INVOICE_ISSUE_REQUESTED],
            "DOCGEN_RESULTS": ["docgen.result.>"],
        }
    )


async def test_subscribe_all_updates_durables_an_earlier_build_created():
    js = _js()
    js.seed(SUBJECT_RUN_REQUESTED, dispatcher._RUN_DURABLE)
    js.seed(SUBJECT_INVOICE_ISSUE_REQUESTED, dispatcher._ISSUE_DURABLE)
    js.seed(settings.DOCGEN_RESULT_SUBJECT, docgen_results._DOCGEN_DURABLE)

    subscriptions = await dispatcher.subscribe_all(js)

    assert len(subscriptions) == 3
    run = js.stored(SUBJECT_RUN_REQUESTED, dispatcher._RUN_DURABLE)
    assert (run.ack_wait, run.max_deliver) == (
        dispatcher._ACK_WAIT_SECONDS,
        dispatcher._MAX_DELIVER,
    )
    issue = js.stored(SUBJECT_INVOICE_ISSUE_REQUESTED, dispatcher._ISSUE_DURABLE)
    assert (issue.ack_wait, issue.max_deliver) == (
        dispatcher._ISSUE_ACK_WAIT_SECONDS,
        dispatcher._MAX_DELIVER,
    )
    result = js.stored(settings.DOCGEN_RESULT_SUBJECT, docgen_results._DOCGEN_DURABLE)
    # One above the handler's own bound, so the handler's DLQ fires first.
    assert (result.ack_wait, result.max_deliver) == (
        docgen_results._ACK_WAIT_SECONDS,
        docgen_results._MAX_DELIVER + 1,
    )


async def test_subscribe_all_binds_each_durable_as_its_own_queue_group():
    js = _js()

    await dispatcher.subscribe_all(js)

    assert [s["durable"] for s in js.subscriptions] == [
        dispatcher._RUN_DURABLE,
        dispatcher._ISSUE_DURABLE,
        docgen_results._DOCGEN_DURABLE,
    ]
    assert all(s["queue"] == s["durable"] and s["manual_ack"] for s in js.subscriptions)
