"""Who may read one invoice: GET /invoices/{id} and GET /invoices/{id}/payments.

A manager (MANAGER or ADMIN) reads any invoice of the community. A plain member
reads only an invoice of a member record they are linked to in the CRM, and gets
403 on anyone else's, the same rule as the PDF download. A missing id stays 404.
"""

from __future__ import annotations

import datetime

import main
from api.billing.deps import get_event_publisher
from tests.factories import crm_billing_factory as f
from worker import persistence

_AUTH = "read-access-test-org"
_FORBIDDEN = 2  # AUTH.FORBIDDEN
_INVOICE_NOT_FOUND = 2221


def _headers(user_id: str, role: str) -> dict[str, str]:
    return {
        "x-user-id": user_id,
        "x-community-id": _AUTH,
        "x-user-orgs": f"[orgId:{_AUTH} orgPath:/x roles:[{role}]]",
    }


_ADMIN = _headers("admin", "ADMIN")
_OWNER = _headers("owner", "MEMBER")


class _FakePublisher:
    async def publish(self, subject: str, event: object) -> None:
        return None


async def _seed(client, db_session) -> tuple[int, int]:
    """Two members A and B, each with an issued invoice and one payment on it.

    Auth user 'owner' is linked to A only. Returns (invoice_a, invoice_b).
    """
    main.app.dependency_overrides[get_event_publisher] = lambda: _FakePublisher()
    cid = await f.create_community(
        db_session, auth_community_id=_AUTH, iban="BE68539007547034", legal_name="ACME ASBL"
    )
    await f.create_subscription(db_session, id_community=cid, feature="billing", is_active=True)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    member_a = await f.create_member(db_session, id_community=cid, name="Alice", member_type=1)
    member_b = await f.create_member(db_session, id_community=cid, name="Bob", member_type=1)
    for ean, member in (("EAN-A", member_a), ("EAN-B", member_b)):
        await f.create_meter(db_session, ean=ean, id_community=cid)
        await f.create_meter_data(
            db_session,
            ean=ean,
            id_community=cid,
            id_sharing_operation=op,
            id_member=member,
            client_type=1,
            start_date=datetime.date(2026, 1, 1),
        )
        await f.create_meter_consumption(
            db_session,
            ean=ean,
            id_community=cid,
            id_sharing_operation=op,
            timestamp=f.june(5),
            shared=20.0,
        )
    owner = await f.create_app_user(db_session, auth_user_id="owner")
    await f.link_user_to_member(db_session, id_user=owner, id_member=member_a)

    await client.post(
        f"/sharing-operations/{op}/tariffs",
        headers=_ADMIN,
        json={"kind": 1, "scope": 1, "price_per_kwh": "0.15", "valid_from": "2026-01-01"},
    )
    resp = await client.post(
        f"/sharing-operations/{op}/billing-runs",
        headers=_ADMIN,
        json={"period_start": "2026-06-01", "period_end": "2026-06-30"},
    )
    run_id = resp.json()["data"]["id"]
    await persistence.process_billing_run(run_id, local_session=db_session, crm_session=db_session)
    resp = await client.get(f"/billing-runs/{run_id}/invoices", headers=_ADMIN)
    by_member = {inv["id_member"]: inv["id"] for inv in resp.json()["data"]}

    for invoice_id in by_member.values():
        resp = await client.post(f"/invoices/{invoice_id}/issue", headers=_ADMIN)
        assert resp.status_code == 200, resp.text
        resp = await client.post(
            f"/invoices/{invoice_id}/payments", headers=_ADMIN, json={"amount": "1.00"}
        )
        assert resp.status_code == 200, resp.text
    return by_member[member_a], by_member[member_b]


async def test_member_reads_own_invoice_and_its_payments(client, db_session):
    invoice_a, _invoice_b = await _seed(client, db_session)

    resp = await client.get(f"/invoices/{invoice_a}", headers=_OWNER)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["id"] == invoice_a
    assert resp.json()["data"]["lines"], "the detail carries the invoice lines"

    resp = await client.get(f"/invoices/{invoice_a}/payments", headers=_OWNER)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]) == 1


async def test_member_forbidden_on_another_members_invoice(client, db_session):
    _invoice_a, invoice_b = await _seed(client, db_session)

    resp = await client.get(f"/invoices/{invoice_b}", headers=_OWNER)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error_code"] == _FORBIDDEN

    resp = await client.get(f"/invoices/{invoice_b}/payments", headers=_OWNER)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error_code"] == _FORBIDDEN


async def test_unlinked_member_forbidden_on_every_invoice(client, db_session):
    invoice_a, invoice_b = await _seed(client, db_session)
    stranger = _headers("nobody", "MEMBER")

    for invoice_id in (invoice_a, invoice_b):
        for path in (f"/invoices/{invoice_id}", f"/invoices/{invoice_id}/payments"):
            resp = await client.get(path, headers=stranger)
            assert resp.status_code == 403, (path, resp.text)


async def test_manager_and_admin_read_any_invoice(client, db_session):
    invoice_a, invoice_b = await _seed(client, db_session)

    # Neither caller is linked to a member: the role alone grants the read.
    for headers in (_headers("manager", "MANAGER"), _ADMIN):
        for invoice_id in (invoice_a, invoice_b):
            resp = await client.get(f"/invoices/{invoice_id}", headers=headers)
            assert resp.status_code == 200, resp.text
            assert resp.json()["data"]["id"] == invoice_id
            resp = await client.get(f"/invoices/{invoice_id}/payments", headers=headers)
            assert resp.status_code == 200, resp.text
            assert len(resp.json()["data"]) == 1


async def test_missing_invoice_is_still_404_for_a_member(client, db_session):
    invoice_a, invoice_b = await _seed(client, db_session)
    missing = max(invoice_a, invoice_b) + 1000

    for path in (f"/invoices/{missing}", f"/invoices/{missing}/payments"):
        resp = await client.get(path, headers=_OWNER)
        assert resp.status_code == 404, (path, resp.text)
        assert resp.json()["error_code"] == _INVOICE_NOT_FOUND
