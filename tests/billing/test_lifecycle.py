"""Lifecycle: send, payment→PAID, overdue sweep, credit-note."""

from __future__ import annotations

import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import update

import main
from api.billing.deps import get_event_publisher
from api.billing.repository import BillingRepository
from shared.models.local_models import InvoiceModel
from tests.factories import crm_billing_factory as f
from worker import persistence

_AUTH = "lifecycle-test-org"


def _headers() -> dict[str, str]:
    return {
        "x-user-id": "u1",
        "x-community-id": _AUTH,
        "x-user-orgs": f"[orgId:{_AUTH} orgPath:/x roles:[ADMIN]]",
    }


class _FakePublisher:
    async def publish(self, subject: str, event) -> None:
        return None


async def _seed_issued_invoice(client, db_session, *, with_email: bool = True) -> tuple[int, int]:
    main.app.dependency_overrides[get_event_publisher] = lambda: _FakePublisher()
    cid = await f.create_community(
        db_session, auth_community_id=_AUTH, iban="BE68539007547034", legal_name="ACME ASBL"
    )
    await f.create_subscription(db_session, id_community=cid, feature="billing", is_active=True)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    member = await f.create_member(db_session, id_community=cid, name="Alice", member_type=1)
    await f.create_individual(
        db_session, id_member=member, email=("alice@example.be" if with_email else None)
    )
    await f.create_meter(db_session, ean="EAN-C", id_community=cid)
    await f.create_meter_data(
        db_session, ean="EAN-C", id_community=cid, id_sharing_operation=op,
        id_member=member, client_type=1, start_date=datetime.date(2026, 1, 1),
    )
    await f.create_meter_consumption(
        db_session, ean="EAN-C", id_community=cid, id_sharing_operation=op,
        timestamp=f.june(5), shared=30.0,
    )
    await client.post(
        f"/sharing-operations/{op}/tariffs",
        headers=_headers(),
        json={"kind": 1, "scope": 1, "price_per_kwh": "0.15", "valid_from": "2026-01-01"},
    )
    resp = await client.post(
        f"/sharing-operations/{op}/billing-runs",
        headers=_headers(),
        json={"period_start": "2026-06-01", "period_end": "2026-06-30"},
    )
    run_id = resp.json()["data"]["id"]
    await persistence.process_billing_run(run_id, local_session=db_session, crm_session=db_session)
    listed = await client.get(f"/billing-runs/{run_id}/invoices", headers=_headers())
    invoice_id = listed.json()["data"][0]["id"]
    resp = await client.post(f"/invoices/{invoice_id}/issue", headers=_headers())
    assert resp.status_code == 200, resp.text
    return cid, invoice_id


async def _attach_artifact(db_session, invoice_id: int) -> None:
    # ORM update keeps the session's in-memory object in sync (no stale reads).
    await BillingRepository(db_session).attach_artifact(
        invoice_id, uri="s3://optimce-documents/inv.pdf", sha256="abc"
    )


async def test_send_marks_sent(client, db_session):
    _cid, invoice_id = await _seed_issued_invoice(client, db_session)
    await _attach_artifact(db_session, invoice_id)
    resp = await client.post(f"/invoices/{invoice_id}/send", headers=_headers())
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == 2  # SENT


async def test_send_without_pdf_fails(client, db_session):
    _cid, invoice_id = await _seed_issued_invoice(client, db_session)
    resp = await client.post(f"/invoices/{invoice_id}/send", headers=_headers())
    assert resp.status_code == 422
    assert resp.json()["error_code"] == 2227  # INVOICE_PDF_NOT_READY


async def test_send_without_email_fails(client, db_session):
    _cid, invoice_id = await _seed_issued_invoice(client, db_session, with_email=False)
    await _attach_artifact(db_session, invoice_id)
    resp = await client.post(f"/invoices/{invoice_id}/send", headers=_headers())
    assert resp.status_code == 422
    assert resp.json()["error_code"] == 2224  # NO_BILLING_EMAIL


async def test_payment_marks_paid_when_covered(client, db_session):
    _cid, invoice_id = await _seed_issued_invoice(client, db_session)
    # Total is 5.45 (30 kWh x 0.15 = 4.50 + 0.95 VAT).
    resp = await client.post(
        f"/invoices/{invoice_id}/payments", headers=_headers(), json={"amount": "2.00"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == 1  # still ISSUED (partial)

    resp = await client.post(
        f"/invoices/{invoice_id}/payments", headers=_headers(), json={"amount": "3.45"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == 3  # PAID

    resp = await client.get(f"/invoices/{invoice_id}/payments", headers=_headers())
    assert len(resp.json()["data"]) == 2


async def test_overdue_sweep_marks_overdue(client, db_session):
    _cid, invoice_id = await _seed_issued_invoice(client, db_session)
    await db_session.execute(
        update(InvoiceModel)
        .where(InvoiceModel.id == invoice_id)
        .values(due_date=datetime.date(2020, 1, 1))
    )

    resp = await client.post("/billing-runs/overdue-sweep", headers=_headers())
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["marked"] == 1

    db_session.expire_all()
    resp = await client.get(f"/invoices/{invoice_id}", headers=_headers())
    assert resp.json()["data"]["status"] == 4  # OVERDUE


async def test_credit_note_mirrors_and_issues_in_own_series(client, db_session):
    _cid, invoice_id = await _seed_issued_invoice(client, db_session)

    resp = await client.post(
        f"/invoices/{invoice_id}/credit-note", headers=_headers(), json={"reason": "correction"}
    )
    assert resp.status_code == 200, resp.text
    credit_note = resp.json()["data"]
    assert credit_note["type"] == 2  # CREDIT_NOTE
    assert credit_note["status"] == 0  # DRAFT
    assert Decimal(credit_note["total"]) == Decimal("-5.45")
    assert Decimal(credit_note["lines"][0]["amount"]) < 0

    # The credit note issues in its own NC series.
    credit_id = credit_note["id"]
    resp = await client.post(f"/invoices/{credit_id}/issue", headers=_headers())
    assert resp.status_code == 200, resp.text
    year = datetime.datetime.now(datetime.UTC).astimezone(ZoneInfo("Europe/Brussels")).year
    assert resp.json()["data"]["number"] == f"NC-{year}-00001"

    # The original invoice is untouched (still ISSUED).
    resp = await client.get(f"/invoices/{invoice_id}", headers=_headers())
    assert resp.json()["data"]["status"] == 1  # ISSUED


async def test_cannot_credit_a_draft(client, db_session):
    main.app.dependency_overrides[get_event_publisher] = lambda: _FakePublisher()
    cid = await f.create_community(
        db_session, auth_community_id=_AUTH, iban="BE68539007547034", legal_name="ACME ASBL"
    )
    await f.create_subscription(db_session, id_community=cid, feature="billing", is_active=True)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    member = await f.create_member(db_session, id_community=cid, name="Bob", member_type=1)
    await f.create_individual(db_session, id_member=member, email="bob@example.be")
    await f.create_meter(db_session, ean="EAN-D", id_community=cid)
    await f.create_meter_data(
        db_session, ean="EAN-D", id_community=cid, id_sharing_operation=op,
        id_member=member, client_type=1, start_date=datetime.date(2026, 1, 1),
    )
    await f.create_meter_consumption(
        db_session, ean="EAN-D", id_community=cid, id_sharing_operation=op,
        timestamp=f.june(5), shared=10.0,
    )
    await client.post(
        f"/sharing-operations/{op}/tariffs",
        headers=_headers(),
        json={"kind": 1, "scope": 1, "price_per_kwh": "0.15", "valid_from": "2026-01-01"},
    )
    resp = await client.post(
        f"/sharing-operations/{op}/billing-runs",
        headers=_headers(),
        json={"period_start": "2026-06-01", "period_end": "2026-06-30"},
    )
    run_id = resp.json()["data"]["id"]
    await persistence.process_billing_run(run_id, local_session=db_session, crm_session=db_session)
    draft_id = (await client.get(f"/billing-runs/{run_id}/invoices", headers=_headers())).json()[
        "data"
    ][0]["id"]

    resp = await client.post(
        f"/invoices/{draft_id}/credit-note", headers=_headers(), json={"reason": "x"}
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == 2240  # CREDIT_NOTE_TARGET_INVALID
