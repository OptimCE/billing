"""Only a consumer INVOICE is a receivable.

A CREDIT_NOTE cancels an invoice and a PRODUCER_STATEMENT is a payout from the
community to the producer. Both are numbered and issued like an invoice, so
anything that keys on the status alone (the overdue sweep, the "unpaid" counts,
the issue notification, the structured communication) treats them as money the
member owes. These tests pin the per-type behaviour, and the CREDITED state an
invoice reaches once a credit note for it is issued.

One prosumer member (one meter that both consumes and injects shared energy)
gets an INVOICE and a PRODUCER_STATEMENT from the same run, so both types sit
side by side in every test.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select, text, update

import main
from api.billing.deps import get_event_publisher
from core.context_vars import current_internal_community_id
from shared.const import InvoiceStatus, InvoiceType
from shared.models.crm_models import NotificationModel
from shared.models.crm_notification_models import OutboundMessageModel
from shared.models.local_models import InvoiceModel
from tests.factories import crm_billing_factory as f
from worker import docgen_results, persistence
from worker.sweeps import _communities_with_overdue_invoices_unscoped

_AUTH = "doc-types-test-org"
_MEMBER_SUB = "prosumer-sub"


def _headers() -> dict[str, str]:
    return {
        "x-user-id": "admin",
        "x-community-id": _AUTH,
        "x-user-orgs": f"[orgId:{_AUTH} orgPath:/x roles:[ADMIN]]",
    }


def _member_headers() -> dict[str, str]:
    return {
        "x-user-id": _MEMBER_SUB,
        "x-community-id": _AUTH,
        "x-user-orgs": f"[orgId:{_AUTH} orgPath:/x roles:[MEMBER]]",
    }


class _FakePublisher:
    async def publish(self, subject: str, event) -> None:
        return None


async def _seed_prosumer(client, db_session) -> dict[str, int]:
    """One member billed for 30 kWh shared and paid for 15 kWh injected.

    Returns the community, member, linked app_user, and the two DRAFT documents
    the run produced: ``invoice_id`` (INVOICE) and ``statement_id``
    (PRODUCER_STATEMENT).
    """
    main.app.dependency_overrides[get_event_publisher] = lambda: _FakePublisher()
    cid = await f.create_community(
        db_session, auth_community_id=_AUTH, iban="BE68539007547034", legal_name="ACME ASBL"
    )
    await f.create_subscription(db_session, id_community=cid, feature="billing", is_active=True)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    member = await f.create_member(
        db_session, id_community=cid, name="Paula", member_type=1, iban="BE71096123456769"
    )
    await f.create_individual(db_session, id_member=member, email="paula@example.be")
    user_id = await f.create_app_user(db_session, auth_user_id=_MEMBER_SUB)
    await f.link_user_to_member(db_session, id_user=user_id, id_member=member)
    await f.create_meter(db_session, ean="EAN-PRO", id_community=cid)
    await f.create_meter_data(
        db_session,
        ean="EAN-PRO",
        id_community=cid,
        id_sharing_operation=op,
        id_member=member,
        client_type=1,
        start_date=datetime.date(2026, 1, 1),
    )
    await f.create_meter_consumption(
        db_session,
        ean="EAN-PRO",
        id_community=cid,
        id_sharing_operation=op,
        timestamp=f.june(5),
        shared=30.0,
        inj_shared=15.0,
    )
    for kind, price in ((1, "0.15"), (2, "0.08")):
        resp = await client.post(
            f"/sharing-operations/{op}/tariffs",
            headers=_headers(),
            json={"kind": kind, "scope": 1, "price_per_kwh": price, "valid_from": "2026-01-01"},
        )
        assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/sharing-operations/{op}/billing-runs",
        headers=_headers(),
        json={"period_start": "2026-06-01", "period_end": "2026-06-30"},
    )
    run_id = resp.json()["data"]["id"]
    await persistence.process_billing_run(run_id, local_session=db_session, crm_session=db_session)
    listed = await client.get(f"/billing-runs/{run_id}/invoices", headers=_headers())
    by_type = {row["type"]: row["id"] for row in listed.json()["data"]}
    return {
        "cid": cid,
        "member": member,
        "user_id": user_id,
        "invoice_id": by_type[InvoiceType.INVOICE],
        "statement_id": by_type[InvoiceType.PRODUCER_STATEMENT],
    }


async def _issue(client, document_id: int) -> dict:
    resp = await client.post(f"/invoices/{document_id}/issue", headers=_headers())
    assert resp.status_code == 200, resp.text
    data: dict = resp.json()["data"]
    return data


async def _credit(client, invoice_id: int) -> dict:
    resp = await client.post(
        f"/invoices/{invoice_id}/credit-note", headers=_headers(), json={"reason": "correction"}
    )
    assert resp.status_code == 200, resp.text
    data: dict = resp.json()["data"]
    return data


async def _get(client, document_id: int) -> dict:
    resp = await client.get(f"/invoices/{document_id}", headers=_headers())
    assert resp.status_code == 200, resp.text
    data: dict = resp.json()["data"]
    return data


async def _notifications(db_session, type_: str) -> list[NotificationModel]:
    result = await db_session.execute(
        select(NotificationModel)
        .where(NotificationModel.type == type_)
        .order_by(NotificationModel.id)
    )
    return list(result.scalars().all())


async def _make_past_due(db_session, *document_ids: int) -> None:
    await db_session.execute(
        update(InvoiceModel)
        .where(InvoiceModel.id.in_(document_ids))
        .values(due_date=datetime.date(2020, 1, 1))
    )


# ---- issue: structured communication and due date --------------------------


async def test_only_the_consumer_invoice_gets_a_structured_communication(client, db_session):
    ids = await _seed_prosumer(client, db_session)
    invoice = await _issue(client, ids["invoice_id"])
    statement = await _issue(client, ids["statement_id"])
    credit = await _issue(client, (await _credit(client, ids["invoice_id"]))["id"])

    # The member pays the invoice, quoting the reference, by its due date.
    assert invoice["structured_comm"] is not None
    assert invoice["due_date"] is not None
    # The community pays the producer: no reference for the producer to pay
    # with, but the due date stays, as the date the community owes the payout.
    assert statement["structured_comm"] is None
    assert statement["due_date"] is not None
    # Nobody pays a credit note.
    assert credit["structured_comm"] is None
    assert credit["due_date"] is None

    # Persisted, not just echoed by the issue response.
    db_session.expire_all()
    assert (await _get(client, ids["statement_id"]))["structured_comm"] is None
    stored_credit = await _get(client, credit["id"])
    assert stored_credit["structured_comm"] is None
    assert stored_credit["due_date"] is None


# ---- issue: notifications ---------------------------------------------------


async def test_each_document_type_is_announced_with_its_own_notification(client, db_session):
    ids = await _seed_prosumer(client, db_session)
    invoice = await _issue(client, ids["invoice_id"])
    statement = await _issue(client, ids["statement_id"])
    credit = await _issue(client, (await _credit(client, ids["invoice_id"]))["id"])
    statement_total = (await _get(client, ids["statement_id"]))["total"]

    issued = await _notifications(db_session, "invoice.issued")
    assert [n.data["invoice_id"] for n in issued] == [ids["invoice_id"]]

    (statement_notification,) = await _notifications(db_session, "producer_statement.issued")
    assert statement_notification.id_user == ids["user_id"]
    # Exact: `data` is JSONB, so a Decimal or a date in it drops the
    # notification silently inside publish's savepoint.
    assert statement_notification.data == {
        "invoice_id": ids["statement_id"],
        "number": statement["number"],
        "total": statement_total,
        "currency": "EUR",
    }

    (credit_notification,) = await _notifications(db_session, "credit_note.issued")
    assert credit_notification.id_user == ids["user_id"]
    assert credit_notification.data == {
        "invoice_id": credit["id"],
        "number": credit["number"],
        "amount": "5.45",  # the magnitude of the -5.45 credit note
        "currency": "EUR",
        "corrects_number": invoice["number"],
    }

    queued = await db_session.execute(
        select(OutboundMessageModel.type).order_by(OutboundMessageModel.id)
    )
    assert list(queued.scalars().all()) == [
        "invoice.issued",
        "producer_statement.issued",
        "credit_note.issued",
    ]


# ---- overdue sweep ----------------------------------------------------------


async def test_overdue_sweep_ignores_a_past_due_producer_statement(client, db_session):
    ids = await _seed_prosumer(client, db_session)
    await _issue(client, ids["invoice_id"])
    await _issue(client, ids["statement_id"])
    await _make_past_due(db_session, ids["invoice_id"], ids["statement_id"])

    resp = await client.post("/billing-runs/overdue-sweep", headers=_headers())
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["marked"] == 1

    db_session.expire_all()
    assert (await _get(client, ids["invoice_id"]))["status"] == InvoiceStatus.OVERDUE
    # The community is the late payer here; the member owes nothing.
    assert (await _get(client, ids["statement_id"]))["status"] == InvoiceStatus.ISSUED
    overdue = await _notifications(db_session, "invoice.overdue")
    assert [n.data["invoice_id"] for n in overdue] == [ids["invoice_id"]]


async def test_overdue_sweep_ignores_a_credited_invoice_and_its_credit_note(client, db_session):
    ids = await _seed_prosumer(client, db_session)
    await _issue(client, ids["invoice_id"])
    credit = await _issue(client, (await _credit(client, ids["invoice_id"]))["id"])
    # A credit note issued before this change still carries a due date.
    await _make_past_due(db_session, ids["invoice_id"], credit["id"])

    resp = await client.post("/billing-runs/overdue-sweep", headers=_headers())
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["marked"] == 0

    db_session.expire_all()
    assert (await _get(client, ids["invoice_id"]))["status"] == InvoiceStatus.CREDITED
    assert (await _get(client, credit["id"]))["status"] == InvoiceStatus.ISSUED
    assert await _notifications(db_session, "invoice.overdue") == []


async def test_the_scheduled_sweep_does_not_enumerate_a_past_due_producer_statement(
    client, db_session
):
    """The worker picks communities with the same WHERE the sweep itself uses."""
    ids = await _seed_prosumer(client, db_session)
    await _issue(client, ids["statement_id"])
    await _make_past_due(db_session, ids["statement_id"])

    async def _enumerate() -> list[int]:
        token = current_internal_community_id.set(None)
        try:
            return await _communities_with_overdue_invoices_unscoped(
                db_session, today=datetime.date.today()
            )
        finally:
            current_internal_community_id.reset(token)

    assert ids["cid"] not in await _enumerate()

    await _issue(client, ids["invoice_id"])
    await _make_past_due(db_session, ids["invoice_id"])
    assert ids["cid"] in await _enumerate()


# ---- credit notes: the CREDITED state ---------------------------------------


async def test_the_invoice_is_credited_when_its_credit_note_is_issued(client, db_session):
    ids = await _seed_prosumer(client, db_session)
    await _issue(client, ids["invoice_id"])

    draft = await _credit(client, ids["invoice_id"])
    # A DRAFT credit note has no legal effect yet: the invoice still stands.
    assert (await _get(client, ids["invoice_id"]))["status"] == InvoiceStatus.ISSUED

    await _issue(client, draft["id"])
    db_session.expire_all()
    credited = await _get(client, ids["invoice_id"])
    assert credited["status"] == InvoiceStatus.CREDITED
    # The original document itself never changes.
    assert credited["number"] is not None
    assert credited["total"] == "5.45"


async def test_a_credited_invoice_is_neither_payable_nor_sendable(client, db_session):
    ids = await _seed_prosumer(client, db_session)
    await _issue(client, ids["invoice_id"])
    await _issue(client, (await _credit(client, ids["invoice_id"]))["id"])
    await _attach_artifact(db_session, ids["invoice_id"])

    resp = await client.post(
        f"/invoices/{ids['invoice_id']}/payments", headers=_headers(), json={"amount": "5.45"}
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == 2223  # INVOICE_NOT_ISSUED

    resp = await client.post(f"/invoices/{ids['invoice_id']}/send", headers=_headers())
    assert resp.status_code == 409
    assert resp.json()["error_code"] == 2223  # INVOICE_NOT_ISSUED

    # The credit note refers to it, so its PDF is kept.
    resp = await client.delete(f"/invoices/{ids['invoice_id']}/pdf", headers=_headers())
    assert resp.status_code == 409
    assert resp.json()["error_code"] == 2229  # INVOICE_PDF_DELETE_FORBIDDEN


async def test_a_paid_invoice_is_credited_too(client, db_session):
    ids = await _seed_prosumer(client, db_session)
    await _issue(client, ids["invoice_id"])
    resp = await client.post(
        f"/invoices/{ids['invoice_id']}/payments", headers=_headers(), json={"amount": "5.45"}
    )
    assert resp.json()["data"]["status"] == InvoiceStatus.PAID

    await _issue(client, (await _credit(client, ids["invoice_id"]))["id"])
    db_session.expire_all()
    assert (await _get(client, ids["invoice_id"]))["status"] == InvoiceStatus.CREDITED
    # The payment that is now owed back is still on record.
    payments = await client.get(f"/invoices/{ids['invoice_id']}/payments", headers=_headers())
    assert len(payments.json()["data"]) == 1


async def test_an_invoice_is_credited_only_once(client, db_session):
    ids = await _seed_prosumer(client, db_session)
    await _issue(client, ids["invoice_id"])
    draft = await _credit(client, ids["invoice_id"])

    # While the first credit note is still a DRAFT ...
    resp = await client.post(
        f"/invoices/{ids['invoice_id']}/credit-note", headers=_headers(), json={"reason": "again"}
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == 2240  # CREDIT_NOTE_TARGET_INVALID

    # ... and once it is issued.
    await _issue(client, draft["id"])
    resp = await client.post(
        f"/invoices/{ids['invoice_id']}/credit-note", headers=_headers(), json={"reason": "again"}
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == 2240


async def test_a_second_credit_note_draft_is_refused_at_issue_without_a_number(client, db_session):
    """The issue-time guard, for a draft that got past the create-time one.

    That is a credit note drafted before the create-time check existed, or two
    concurrent creates. Issuing it would refund the bill twice; refusing it must
    happen before the NC number is claimed, so the series stays gapless.
    """
    ids = await _seed_prosumer(client, db_session)
    original = await _issue(client, ids["invoice_id"])
    first = await _credit(client, ids["invoice_id"])
    stored = await db_session.get(InvoiceModel, first["id"])
    second = InvoiceModel(
        id_community=ids["cid"],
        id_billing_run=stored.id_billing_run,
        id_member=stored.id_member,
        doc_type=InvoiceType.CREDIT_NOTE,
        status=InvoiceStatus.DRAFT,
        legal_entity_key=stored.legal_entity_key,
        currency=stored.currency,
        subtotal=stored.subtotal,
        vat_rate=stored.vat_rate,
        vat_amount=stored.vat_amount,
        total=stored.total,
        corrects_invoice_id=ids["invoice_id"],
    )
    db_session.add(second)
    await db_session.flush()
    # Read before the refused issue: its rollback expires every loaded instance.
    second_id, credit_series = second.id, stored.legal_entity_key

    issued_first = await _issue(client, first["id"])
    resp = await client.post(f"/invoices/{second_id}/issue", headers=_headers())
    assert resp.status_code == 409, resp.text
    assert resp.json()["error_code"] == 2240  # CREDIT_NOTE_TARGET_INVALID

    db_session.expire_all()
    refused = await _get(client, second_id)
    assert refused["status"] == InvoiceStatus.DRAFT
    assert refused["number"] is None
    last_value = await db_session.execute(
        text("SELECT last_value FROM invoice_sequence WHERE legal_entity_key = :lek"),
        {"lek": credit_series},
    )
    assert last_value.scalar_one() == 1  # only the first credit note was numbered
    assert issued_first["number"].endswith("-00001")
    assert (await _get(client, ids["invoice_id"]))["status"] == InvoiceStatus.CREDITED
    assert original["number"] is not None


# ---- PDF re-render failure never rewrites a settled status -----------------


async def _attach_artifact(db_session, invoice_id: int) -> None:
    await db_session.execute(
        update(InvoiceModel)
        .where(InvoiceModel.id == invoice_id)
        .values(artifact_uri="s3://optimce-documents/inv.pdf", artifact_sha256="abc")
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (InvoiceStatus.ISSUED, InvoiceStatus.RENDER_FAILED),
        (InvoiceStatus.SENT, InvoiceStatus.SENT),
        (InvoiceStatus.PAID, InvoiceStatus.PAID),
        (InvoiceStatus.OVERDUE, InvoiceStatus.OVERDUE),
        (InvoiceStatus.CREDITED, InvoiceStatus.CREDITED),
    ],
)
async def test_a_failed_re_render_only_marks_an_issued_invoice(
    client, db_session, status, expected
):
    """RENDER_FAILED recovers to ISSUED on the next successful render.

    So marking a SENT, PAID, OVERDUE or CREDITED invoice RENDER_FAILED would,
    one retry later, make it payable (and sweepable OVERDUE) again.
    """
    ids = await _seed_prosumer(client, db_session)
    await _issue(client, ids["invoice_id"])
    await db_session.execute(
        update(InvoiceModel)
        .where(InvoiceModel.id == ids["invoice_id"])
        .values(status=status, docgen_request_id="re-render-req")
    )

    outcome = await docgen_results.process_docgen_result(
        {
            "request_id": "re-render-req",
            "tenant_id": str(ids["cid"]),
            "status": "failed",
            "error": {"code": "RENDER_ERROR", "message": "boom", "permanent": True},
        },
        local_session=db_session,
        crm_session=db_session,
    )
    assert outcome == "render_failed"

    db_session.expire_all()
    fresh = await db_session.get(InvoiceModel, ids["invoice_id"])
    assert fresh.status == expected


# ---- list filter: ?type= ----------------------------------------------------


async def test_invoice_list_filters_by_type_in_rows_and_total(client, db_session):
    ids = await _seed_prosumer(client, db_session)

    resp = await client.get("/invoices", headers=_headers())
    assert resp.json()["pagination"]["total"] == 2

    resp = await client.get("/invoices?type=1", headers=_headers())
    body = resp.json()
    assert body["pagination"]["total"] == 1
    assert [row["id"] for row in body["data"]] == [ids["invoice_id"]]

    resp = await client.get("/invoices?type=3", headers=_headers())
    assert [row["id"] for row in resp.json()["data"]] == [ids["statement_id"]]

    # The manager dashboard's "still owed" count: a limit=1 read of the total,
    # so the filter must reach the COUNT query, not just the page.
    await _issue(client, ids["invoice_id"])
    await _issue(client, ids["statement_id"])
    resp = await client.get("/invoices?status=1&limit=1", headers=_headers())
    assert resp.json()["pagination"]["total"] == 2
    resp = await client.get("/invoices?status=1&type=1&limit=1", headers=_headers())
    assert resp.json()["pagination"]["total"] == 1


async def test_my_invoices_filters_by_type_in_rows_and_total(client, db_session):
    ids = await _seed_prosumer(client, db_session)

    resp = await client.get("/invoices/mine", headers=_member_headers())
    assert resp.json()["pagination"]["total"] == 2

    resp = await client.get("/invoices/mine?type=1&limit=1", headers=_member_headers())
    body = resp.json()
    assert body["pagination"]["total"] == 1
    assert [row["id"] for row in body["data"]] == [ids["invoice_id"]]

    resp = await client.get("/invoices/mine?type=2", headers=_member_headers())
    assert resp.json()["pagination"]["total"] == 0


pytestmark = pytest.mark.asyncio
