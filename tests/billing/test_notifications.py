"""Notifications this service writes into the shared CRM ``notification`` table.

Three producers (IMPLEMENTATION_PLAN §1.4): ``invoice.issued`` and
``invoice.overdue`` reach the member the invoice bills, ``billing_run.completed``
reaches the community's managers. All three ride on the caller's CRM session and
are staged before the CRM commit, so they share one transaction with the audit
row. ``get_crm_session`` and ``get_local_session`` both resolve to ``db_session``
here, so the rows are visible to the test without a commit.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select, update

import main
from api.billing.deps import get_event_publisher
from core.database.models import AuditLogModel
from core.notifications import Channel, NotificationCategory, UsersTarget
from core.notifications.repository import NotificationRepository
from core.notifications.service import EmailRecipient, NotificationService
from shared.models.crm_models import NotificationModel
from shared.models.crm_notification_models import (
    NotificationPreferenceModel,
    OutboundMessageModel,
)
from shared.models.local_models import InvoiceModel
from tests.factories import crm_billing_factory as f
from worker import persistence

_AUTH = "notification-test-org"


def _headers() -> dict[str, str]:
    return {
        "x-user-id": "u1",
        "x-community-id": _AUTH,
        "x-user-orgs": f"[orgId:{_AUTH} orgPath:/x roles:[ADMIN]]",
    }


class _FakePublisher:
    async def publish(self, subject: str, event) -> None:
        return None


async def _notifications(db_session) -> list[NotificationModel]:
    result = await db_session.execute(select(NotificationModel).order_by(NotificationModel.id))
    return list(result.scalars().all())


async def _outbound(db_session) -> list[OutboundMessageModel]:
    result = await db_session.execute(
        select(OutboundMessageModel).order_by(OutboundMessageModel.id)
    )
    return list(result.scalars().all())


async def _seed_run(
    db_session,
    client,
    *,
    link_user: bool = True,
    roster: dict[str, str] | None = None,
) -> dict[str, int]:
    """A community with one billed member, priced consumption and a COMPUTED run.

    Returns the ids the tests assert on. ``link_user=False`` models a member
    invoiced on paper: they exist, but no portal account represents them.
    ``roster`` seeds ``community_user`` rows (``{auth_id: role}``) *before* the
    run is processed, so the ``billing_run.completed`` fan-out sees them; their
    internal ids come back under the same keys.
    """
    main.app.dependency_overrides[get_event_publisher] = lambda: _FakePublisher()
    cid = await f.create_community(
        db_session, auth_community_id=_AUTH, iban="BE68539007547034", legal_name="ACME ASBL"
    )
    await f.create_subscription(db_session, id_community=cid, feature="billing", is_active=True)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    member = await f.create_member(db_session, id_community=cid, name="Alice", member_type=1)
    await f.create_individual(db_session, id_member=member, email="alice@example.be")
    user_id = await f.create_app_user(db_session, auth_user_id="alice-sub")
    if link_user:
        await f.link_user_to_member(db_session, id_user=user_id, id_member=member)
    await f.create_meter(db_session, ean="EAN-N", id_community=cid)
    await f.create_meter_data(
        db_session,
        ean="EAN-N",
        id_community=cid,
        id_sharing_operation=op,
        id_member=member,
        client_type=1,
        start_date=datetime.date(2026, 1, 1),
    )
    await f.create_meter_consumption(
        db_session,
        ean="EAN-N",
        id_community=cid,
        id_sharing_operation=op,
        timestamp=f.june(5),
        shared=30.0,
    )
    await client.post(
        f"/sharing-operations/{op}/tariffs",
        headers=_headers(),
        json={"kind": 1, "scope": 1, "price_per_kwh": "0.15", "valid_from": "2026-01-01"},
    )
    ids: dict[str, int] = {}
    for auth_id, role in (roster or {}).items():
        uid = await f.create_app_user(db_session, auth_user_id=auth_id)
        await f.add_community_member(db_session, id_community=cid, id_user=uid, role=role)
        ids[auth_id] = uid
    resp = await client.post(
        f"/sharing-operations/{op}/billing-runs",
        headers=_headers(),
        json={"period_start": "2026-06-01", "period_end": "2026-06-30"},
    )
    run_id = resp.json()["data"]["id"]
    await persistence.process_billing_run(run_id, local_session=db_session, crm_session=db_session)
    listed = await client.get(f"/billing-runs/{run_id}/invoices", headers=_headers())
    ids.update(
        cid=cid,
        run_id=run_id,
        member=member,
        user_id=user_id,
        invoice_id=listed.json()["data"][0]["id"],
    )
    return ids


# ---- invoice.issued --------------------------------------------------------


async def test_issue_invoice_notifies_the_invoiced_member(client, db_session):
    ids = await _seed_run(db_session, client)

    resp = await client.post(f"/invoices/{ids['invoice_id']}/issue", headers=_headers())
    assert resp.status_code == 200, resp.text
    issued = resp.json()["data"]

    notifs = [n for n in await _notifications(db_session) if n.type == "invoice.issued"]
    assert len(notifs) == 1
    assert notifs[0].id_user == ids["user_id"]
    assert notifs[0].id_community == ids["cid"]
    assert notifs[0].read_at is None
    # Compared exactly: `data` is JSONB serialised with plain json.dumps, which
    # raises on Decimal and date. That raise happens inside publish's savepoint
    # and inside its blanket except, so a non-primitive payload silently drops
    # the notification behind a 200. This assertion is the only thing that sees it.
    assert notifs[0].data == {
        "invoice_id": ids["invoice_id"],
        "number": issued["number"],
        "due_date": issued["due_date"],
        "total": "5.45",  # 30 kWh at 0.15 plus VAT, stringified from Decimal
        "currency": "EUR",
    }


async def test_issue_invoice_with_unlinked_member_notifies_nobody(client, db_session):
    ids = await _seed_run(db_session, client, link_user=False)

    resp = await client.post(f"/invoices/{ids['invoice_id']}/issue", headers=_headers())
    assert resp.status_code == 200, resp.text
    assert await _notifications(db_session) == []


async def test_notification_failure_does_not_abort_issue(client, db_session, monkeypatch):
    """The SAVEPOINT, not the swallow, is what keeps the CRM transaction usable.

    The audit row is staged on the same CRM session *before* the notification. If
    a failed insert poisoned that transaction, the audit row would vanish too —
    which is the specific risk of hooking publish after the audit call.
    """
    ids = await _seed_run(db_session, client)

    async def _boom(self: NotificationRepository, rows: list[NotificationModel]) -> None:
        raise RuntimeError("notification store unavailable")

    monkeypatch.setattr(NotificationRepository, "insert_many", _boom)

    resp = await client.post(f"/invoices/{ids['invoice_id']}/issue", headers=_headers())
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == 1  # ISSUED, with its number claimed

    # The rolled-back savepoint leaves no notification rows ...
    assert await _notifications(db_session) == []
    # ... and the sibling audit row, staged on the same CRM transaction *before*
    # the notification, survived it.
    audit = await db_session.execute(
        select(AuditLogModel.action).where(AuditLogModel.entity_id == str(ids["invoice_id"]))
    )
    assert "billing.invoice.issued" in list(audit.scalars().all())
    # ... and the invoice really is numbered.
    number = await db_session.execute(
        select(InvoiceModel.number).where(InvoiceModel.id == ids["invoice_id"])
    )
    assert number.scalar_one() is not None


async def test_issue_invoice_queues_the_email(client, db_session):
    """The real enqueue, not the spy: EMAIL now produces an `outbound_message`.

    The address, display name and locale are resolved HERE and copied onto the
    row, so a later profile change never redirects an already-queued message.
    """
    ids = await _seed_run(db_session, client)

    resp = await client.post(f"/invoices/{ids['invoice_id']}/issue", headers=_headers())
    assert resp.status_code == 200, resp.text

    queued = await _outbound(db_session)
    assert len(queued) == 1
    row = queued[0]
    assert row.type == "invoice.issued"
    assert row.channel == int(Channel.EMAIL)
    assert row.category == int(NotificationCategory.TRANSACTIONAL)
    assert row.id_community == ids["cid"]
    assert row.status == 1  # PENDING
    assert row.attempts == 0
    # The link back to the in-app row is what `insert_many`'s flush buys.
    notifs = [n for n in await _notifications(db_session) if n.type == "invoice.issued"]
    assert row.id_notification == notifs[0].id
    # No locale is seeded, so '' means "unknown" and the dispatcher's fallback
    # chain decides — it is the only component that knows which locales it has
    # templates for.
    assert row.locale == ""


async def test_queued_email_is_idempotent_on_the_dedupe_key(client, db_session):
    """A replayed publish collapses to one queued message.

    The likeliest implementation bug is deriving the key from `id_notification`,
    which would make every key unique and the whole mechanism a silent no-op.
    """
    ids = await _seed_run(db_session, client)
    await client.post(f"/invoices/{ids['invoice_id']}/issue", headers=_headers())

    service = NotificationService(db_session)
    payload = {"invoice_id": ids["invoice_id"], "number": "X"}
    for _ in range(2):
        await service.publish(
            type="invoice.issued",
            target=UsersTarget(user_ids=[ids["user_id"]], community_id=ids["cid"]),
            category=NotificationCategory.TRANSACTIONAL,
            channels=(Channel.INAPP, Channel.EMAIL),
            data=payload,
        )

    # One from the issue above plus one from the pair of identical publishes.
    assert len(await _outbound(db_session)) == 2


async def test_queued_email_is_skipped_for_an_unlinked_member(client, db_session):
    """A member invoiced on paper has no account, so there is nothing to email."""
    ids = await _seed_run(db_session, client, link_user=False)

    resp = await client.post(f"/invoices/{ids['invoice_id']}/issue", headers=_headers())
    assert resp.status_code == 200, resp.text
    assert await _outbound(db_session) == []


async def test_enqueue_failure_does_not_abort_issue(client, db_session, monkeypatch):
    """The savepoint covers the enqueue too, not just the notification insert."""
    ids = await _seed_run(db_session, client)

    async def _boom(self: NotificationRepository, rows: list[dict[str, object]]) -> None:
        raise RuntimeError("outbound queue unavailable")

    monkeypatch.setattr(NotificationRepository, "insert_outbound", _boom)

    resp = await client.post(f"/invoices/{ids['invoice_id']}/issue", headers=_headers())
    assert resp.status_code == 200, resp.text

    # The whole savepoint rolled back, so neither half of the publish survived...
    assert await _outbound(db_session) == []
    assert [n for n in await _notifications(db_session) if n.type == "invoice.issued"] == []
    # ... and the audit row staged before it on the same CRM transaction did.
    audit = await db_session.execute(
        select(AuditLogModel.action).where(AuditLogModel.entity_id == str(ids["invoice_id"]))
    )
    assert "billing.invoice.issued" in list(audit.scalars().all())


async def test_preference_off_mutes_informational_but_never_transactional(client, db_session):
    """The pair is the point.

    A preference that silenced an invoice would be a compliance bug, not a
    feature — TRANSACTIONAL skips the preference lookup entirely.
    """
    ids = await _seed_run(db_session, client)
    db_session.add(
        NotificationPreferenceModel(
            id_user=ids["user_id"], type_prefix="", channel=int(Channel.EMAIL), mode=3
        )
    )
    await db_session.flush()

    service = NotificationService(db_session)
    informational = await service.publish(
        type="invoice.issued",
        target=UsersTarget(user_ids=[ids["user_id"]], community_id=ids["cid"]),
        category=NotificationCategory.INFORMATIONAL,
        channels=(Channel.INAPP, Channel.EMAIL),
        data={"invoice_id": 1},
    )
    assert informational == 1
    assert await _outbound(db_session) == []

    transactional = await service.publish(
        type="invoice.issued",
        target=UsersTarget(user_ids=[ids["user_id"]], community_id=ids["cid"]),
        category=NotificationCategory.TRANSACTIONAL,
        channels=(Channel.INAPP, Channel.EMAIL),
        data={"invoice_id": 2},
    )
    assert transactional == 1
    assert len(await _outbound(db_session)) == 1


async def test_email_channel_reaches_the_enqueue_seam(client, db_session, monkeypatch):
    """`_enqueue_email` is reached with the recipients already resolved.

    Grepping for it still finds every place the delivery layer hangs off. The
    non-None `id_notification` proves the in-app rows were flushed first, which
    is what `outbound_message.id_notification` needs.
    """
    ids = await _seed_run(db_session, client)
    calls: list[dict[str, object]] = []

    async def _spy(self: NotificationService, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(NotificationService, "_enqueue_email", _spy)

    resp = await client.post(f"/invoices/{ids['invoice_id']}/issue", headers=_headers())
    assert resp.status_code == 200, resp.text

    assert len(calls) == 1
    assert calls[0]["type"] == "invoice.issued"
    assert calls[0]["category"] is NotificationCategory.TRANSACTIONAL
    recipients = calls[0]["recipients"]
    assert isinstance(recipients, list)
    assert [r.id_user for r in recipients] == [ids["user_id"]]
    assert all(isinstance(r, EmailRecipient) and r.id_notification is not None for r in recipients)


# ---- invoice.overdue -------------------------------------------------------


async def test_overdue_sweep_notifies_the_invoiced_member(client, db_session):
    ids = await _seed_run(db_session, client)
    await client.post(f"/invoices/{ids['invoice_id']}/issue", headers=_headers())
    await db_session.execute(
        update(InvoiceModel)
        .where(InvoiceModel.id == ids["invoice_id"])
        .values(due_date=datetime.date(2020, 1, 1))
    )

    resp = await client.post("/billing-runs/overdue-sweep", headers=_headers())
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["marked"] == 1

    overdue = [n for n in await _notifications(db_session) if n.type == "invoice.overdue"]
    assert len(overdue) == 1
    assert overdue[0].id_user == ids["user_id"]
    assert overdue[0].data["invoice_id"] == ids["invoice_id"]


async def test_second_overdue_sweep_notifies_nobody(client, db_session):
    """Idempotency comes free: the rows are already OVERDUE, so nothing matches."""
    ids = await _seed_run(db_session, client)
    await client.post(f"/invoices/{ids['invoice_id']}/issue", headers=_headers())
    await db_session.execute(
        update(InvoiceModel)
        .where(InvoiceModel.id == ids["invoice_id"])
        .values(due_date=datetime.date(2020, 1, 1))
    )
    await client.post("/billing-runs/overdue-sweep", headers=_headers())

    resp = await client.post("/billing-runs/overdue-sweep", headers=_headers())
    assert resp.json()["data"]["marked"] == 0

    overdue = [n for n in await _notifications(db_session) if n.type == "invoice.overdue"]
    assert len(overdue) == 1


# ---- billing_run.completed -------------------------------------------------


_ROSTER = {"admin-sub": "ADMIN", "manager-sub": "MANAGER", "member-sub": "MEMBER"}


async def test_billing_run_completed_notifies_managers_only(client, db_session):
    ids = await _seed_run(db_session, client, roster=_ROSTER)

    completed = [n for n in await _notifications(db_session) if n.type == "billing_run.completed"]
    assert {n.id_user for n in completed} == {ids["admin-sub"], ids["manager-sub"]}
    assert ids["member-sub"] not in {n.id_user for n in completed}
    assert all(n.id_community == ids["cid"] for n in completed)
    assert all(n.data == {"run_id": ids["run_id"], "invoice_count": 1} for n in completed)


async def test_redelivered_billing_run_does_not_re_notify(client, db_session):
    """A redelivery finds the run already COMPUTED and returns before notifying."""
    ids = await _seed_run(db_session, client, roster={"manager-sub": "MANAGER"})

    await persistence.process_billing_run(
        ids["run_id"], local_session=db_session, crm_session=db_session
    )

    completed = [n for n in await _notifications(db_session) if n.type == "billing_run.completed"]
    assert len(completed) == 1


pytestmark = pytest.mark.asyncio
