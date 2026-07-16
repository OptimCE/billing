"""Caller-scoped member invoices: GET /invoices/mine.

A logged-in user (identified by the x-user-id header the gateway injects) sees
only the invoices of the member(s) they are linked to via the CRM
user_member_link — never anyone else's, even if they try to pass ?participant.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import main
from api.billing.deps import get_event_publisher
from tests.factories import crm_billing_factory as f
from worker import persistence

_AUTH = "mine-test-org"


def _headers(user_id: str) -> dict[str, str]:
    return {
        "x-user-id": user_id,
        "x-community-id": _AUTH,
        "x-user-orgs": f"[orgId:{_AUTH} orgPath:/x roles:[MEMBER]]",
    }


def _admin_headers() -> dict[str, str]:
    return {
        "x-user-id": "admin",
        "x-community-id": _AUTH,
        "x-user-orgs": f"[orgId:{_AUTH} orgPath:/x roles:[ADMIN]]",
    }


class _FakePublisher:
    async def publish(self, subject: str, event) -> None:
        return None


async def _seed(client, db_session) -> tuple[int, int, int]:
    """Community with two members A and B (each with a DRAFT invoice); user 'user-a' linked to A."""
    main.app.dependency_overrides[get_event_publisher] = lambda: _FakePublisher()
    cid = await f.create_community(db_session, auth_community_id=_AUTH)
    await f.create_subscription(db_session, id_community=cid, feature="billing", is_active=True)
    op = await f.create_sharing_operation(db_session, id_community=cid)

    member_a = await f.create_member(db_session, id_community=cid, name="Alice")
    member_b = await f.create_member(db_session, id_community=cid, name="Bob")
    for ean, member in (("EAN-A", member_a), ("EAN-B", member_b)):
        await f.create_meter(db_session, ean=ean, id_community=cid)
        await f.create_meter_data(
            db_session, ean=ean, id_community=cid, id_sharing_operation=op,
            id_member=member, client_type=1, start_date=datetime.date(2026, 1, 1),
        )
        await f.create_meter_consumption(
            db_session, ean=ean, id_community=cid, id_sharing_operation=op,
            timestamp=f.june(5), shared=20.0,
        )

    # Link auth user "user-a" to member A only.
    user_a = await f.create_app_user(db_session, auth_user_id="user-a")
    await f.link_user_to_member(db_session, id_user=user_a, id_member=member_a)

    await client.post(
        f"/sharing-operations/{op}/tariffs",
        headers=_admin_headers(),
        json={"kind": 1, "scope": 1, "price_per_kwh": "0.15", "valid_from": "2026-01-01"},
    )
    resp = await client.post(
        f"/sharing-operations/{op}/billing-runs",
        headers=_admin_headers(),
        json={"period_start": "2026-06-01", "period_end": "2026-06-30"},
    )
    run_id = resp.json()["data"]["id"]
    await persistence.process_billing_run(run_id, local_session=db_session, crm_session=db_session)
    return cid, member_a, member_b


async def test_mine_returns_only_callers_invoices(client, db_session):
    _cid, member_a, member_b = await _seed(client, db_session)

    resp = await client.get("/invoices/mine", headers=_headers("user-a"))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["id_member"] == member_a
    assert data[0]["id_member"] != member_b


async def test_mine_ignores_participant_query(client, db_session):
    _cid, member_a, member_b = await _seed(client, db_session)

    # Trying to target member B is ignored — the route is caller-scoped, not client-filtered.
    resp = await client.get(f"/invoices/mine?participant={member_b}", headers=_headers("user-a"))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["id_member"] == member_a


async def test_mine_empty_for_unlinked_user(client, db_session):
    await _seed(client, db_session)

    resp = await client.get("/invoices/mine", headers=_headers("nobody"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"] == []
    assert body["pagination"]["total"] == 0


async def test_mine_status_filter(client, db_session):
    await _seed(client, db_session)

    # A's invoice is DRAFT (status 0).
    resp = await client.get("/invoices/mine?status=0", headers=_headers("user-a"))
    assert len(resp.json()["data"]) == 1
    resp = await client.get("/invoices/mine?status=1", headers=_headers("user-a"))
    assert len(resp.json()["data"]) == 0


async def _seed_two_for_user(client, db_session) -> None:
    """user-a linked to two members with DISTINCT invoice totals (for sort tests)."""
    main.app.dependency_overrides[get_event_publisher] = lambda: _FakePublisher()
    cid = await f.create_community(db_session, auth_community_id=_AUTH)
    await f.create_subscription(db_session, id_community=cid, feature="billing", is_active=True)
    op = await f.create_sharing_operation(db_session, id_community=cid)

    user_a = await f.create_app_user(db_session, auth_user_id="user-a")
    # Alice 20 kWh, Bob 40 kWh → Bob's invoice total is strictly larger.
    for ean, name, kwh in (("EAN-A", "Alice", 20.0), ("EAN-B", "Bob", 40.0)):
        member = await f.create_member(db_session, id_community=cid, name=name)
        await f.create_meter(db_session, ean=ean, id_community=cid)
        await f.create_meter_data(
            db_session, ean=ean, id_community=cid, id_sharing_operation=op,
            id_member=member, client_type=1, start_date=datetime.date(2026, 1, 1),
        )
        await f.create_meter_consumption(
            db_session, ean=ean, id_community=cid, id_sharing_operation=op,
            timestamp=f.june(5), shared=kwh,
        )
        await f.link_user_to_member(db_session, id_user=user_a, id_member=member)

    await client.post(
        f"/sharing-operations/{op}/tariffs",
        headers=_admin_headers(),
        json={"kind": 1, "scope": 1, "price_per_kwh": "0.15", "valid_from": "2026-01-01"},
    )
    resp = await client.post(
        f"/sharing-operations/{op}/billing-runs",
        headers=_admin_headers(),
        json={"period_start": "2026-06-01", "period_end": "2026-06-30"},
    )
    run_id = resp.json()["data"]["id"]
    await persistence.process_billing_run(run_id, local_session=db_session, crm_session=db_session)


async def test_mine_sort_by_total(client, db_session):
    await _seed_two_for_user(client, db_session)

    asc = await client.get("/invoices/mine?sort=total&order=asc", headers=_headers("user-a"))
    assert asc.status_code == 200, asc.text
    totals_asc = [Decimal(row["total"]) for row in asc.json()["data"]]
    assert len(totals_asc) == 2
    assert totals_asc == sorted(totals_asc)  # ascending

    desc = await client.get("/invoices/mine?sort=total&order=desc", headers=_headers("user-a"))
    totals_desc = [Decimal(row["total"]) for row in desc.json()["data"]]
    assert totals_desc == sorted(totals_desc, reverse=True)  # descending
    assert totals_asc[0] == totals_desc[-1]  # same set, opposite order


async def test_mine_issued_date_filter(client, db_session):
    await _seed(client, db_session)

    # Issue member A's DRAFT invoice so issued_at is populated.
    invoice_id = (await client.get("/invoices/mine", headers=_headers("user-a"))).json()["data"][
        0
    ]["id"]
    resp = await client.post(f"/invoices/{invoice_id}/issue", headers=_admin_headers())
    assert resp.status_code == 200, resp.text

    listed = await client.get("/invoices/mine", headers=_headers("user-a"))
    issued_at = listed.json()["data"][0]["issued_at"]
    issued_date = datetime.datetime.fromisoformat(issued_at).date()
    day_after = issued_date + datetime.timedelta(days=1)
    day_before = issued_date - datetime.timedelta(days=1)

    # issued_from is inclusive of the issue day; the day after excludes it.
    resp = await client.get(
        f"/invoices/mine?issued_from={issued_date.isoformat()}", headers=_headers("user-a")
    )
    assert resp.json()["pagination"]["total"] == 1
    resp = await client.get(
        f"/invoices/mine?issued_from={day_after.isoformat()}", headers=_headers("user-a")
    )
    assert resp.json()["pagination"]["total"] == 0

    # issued_to is inclusive of the whole issue day; the day before excludes it.
    resp = await client.get(
        f"/invoices/mine?issued_to={issued_date.isoformat()}", headers=_headers("user-a")
    )
    assert resp.json()["pagination"]["total"] == 1
    resp = await client.get(
        f"/invoices/mine?issued_to={day_before.isoformat()}", headers=_headers("user-a")
    )
    assert resp.json()["pagination"]["total"] == 0
