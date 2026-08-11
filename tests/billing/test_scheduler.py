"""The overdue scheduler: tenant enumeration, the lock, and the next-run clock.

`POST /billing-runs/overdue-sweep` existed from the start and nothing ever
invoked it, so no invoice was ever marked overdue on its own and
`invoice.overdue` — TRANSACTIONAL, to the member's inbox — could not fire.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text, update

from core.context_vars import current_internal_community_id
from shared.models.local_models import InvoiceModel
from tests.billing.test_notifications import _headers, _notifications, _outbound, _seed_run
from worker.scheduler import seconds_until_next_run
from worker.sweeps import (
    _communities_with_overdue_invoices_unscoped,
    sweep_overdue_for_every_community,
)

BRUSSELS = ZoneInfo("Europe/Brussels")


class TestNextRunClock:
    """Pure, so the wrap-around cases are testable without waiting a day."""

    def test_a_time_before_the_hour_waits_until_today(self):
        now = datetime.datetime(2026, 8, 3, 4, 0, tzinfo=BRUSSELS)
        assert seconds_until_next_run(now, 6) == 2 * 3600

    def test_a_time_after_the_hour_waits_until_tomorrow(self):
        now = datetime.datetime(2026, 8, 3, 7, 0, tzinfo=BRUSSELS)
        assert seconds_until_next_run(now, 6) == 23 * 3600

    def test_a_utc_instant_is_converted_before_comparing(self):
        """The hour is a LOCAL one; comparing in UTC would drift with DST."""
        now = datetime.datetime(2026, 8, 3, 3, 0, tzinfo=datetime.UTC)
        assert seconds_until_next_run(now, 6) == 3600


async def _issue_and_backdate(client, db_session) -> dict[str, int]:
    ids = await _seed_run(db_session, client)
    await client.post(f"/invoices/{ids['invoice_id']}/issue", headers=_headers())
    await db_session.execute(
        update(InvoiceModel)
        .where(InvoiceModel.id == ids["invoice_id"])
        .values(due_date=datetime.date(2020, 1, 1))
    )
    await db_session.flush()
    return ids


async def test_the_enumeration_is_unscoped_and_finds_work_with_no_tenant_set(client, db_session):
    """The failure this guards against is silent.

    Every owned-DB read in the service filters on the
    `current_internal_community_id` ContextVar, which a scheduler has not set —
    and a scoped read returns nothing, with no error, which is
    indistinguishable from "no work to do".
    """
    ids = await _issue_and_backdate(client, db_session)
    token = current_internal_community_id.set(None)
    try:
        found = await _communities_with_overdue_invoices_unscoped(
            db_session, today=datetime.date.today()
        )
    finally:
        current_internal_community_id.reset(token)
    assert ids["cid"] in found


async def test_the_scheduled_sweep_marks_and_notifies_like_the_route(client, db_session):
    """One implementation, two callers — the point of not forking the sweep."""
    ids = await _issue_and_backdate(client, db_session)
    current_internal_community_id.set(None)

    marked, swept = await sweep_overdue_for_every_community(
        local_session=db_session, crm_session=db_session
    )

    assert (marked, swept) == (1, 1)
    overdue = [n for n in await _notifications(db_session) if n.type == "invoice.overdue"]
    assert len(overdue) == 1
    assert overdue[0].id_user == ids["user_id"]
    # And the email half is queued, which is the whole reason the sweep matters.
    assert [row.type for row in await _outbound(db_session)] == [
        "invoice.issued",
        "invoice.overdue",
    ]


async def test_a_second_scheduled_run_is_a_no_op(client, db_session):
    """Re-running on a deploy must not re-notify: the invoices are already
    OVERDUE, so the UPDATE matches nothing."""
    await _issue_and_backdate(client, db_session)
    current_internal_community_id.set(None)

    await sweep_overdue_for_every_community(local_session=db_session, crm_session=db_session)
    before = len(await _notifications(db_session))

    marked, _ = await sweep_overdue_for_every_community(
        local_session=db_session, crm_session=db_session
    )
    assert marked == 0
    assert len(await _notifications(db_session)) == before


async def test_nothing_pending_means_no_work(db_session):
    current_internal_community_id.set(None)
    assert await sweep_overdue_for_every_community(
        local_session=db_session, crm_session=db_session
    ) == (0, 0)


async def test_the_advisory_lock_is_visible_as_held(db_session):
    """Two replicas both sweeping would give every member two bell entries:
    `dedupe_key` collapses the duplicate email, nothing collapses the in-app row."""
    from worker.sweeps import _SWEEP_ADVISORY_LOCK_KEY

    acquired = await db_session.scalar(
        text("SELECT pg_try_advisory_lock(:k)"), {"k": _SWEEP_ADVISORY_LOCK_KEY}
    )
    assert acquired is True
    try:
        held = await db_session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks " "WHERE locktype = 'advisory' AND granted)"
            )
        )
        assert held is True
    finally:
        await db_session.execute(
            text("SELECT pg_advisory_unlock(:k)"), {"k": _SWEEP_ADVISORY_LOCK_KEY}
        )


@pytest.mark.parametrize("other_key", [0x0AD3_0001])
def test_the_billing_and_deadline_locks_do_not_collide(other_key: int):
    """Both workers run against the same cluster; a shared key would serialise
    two unrelated sweeps and silently hide one of them."""
    from worker.sweeps import _SWEEP_ADVISORY_LOCK_KEY

    assert other_key != _SWEEP_ADVISORY_LOCK_KEY
