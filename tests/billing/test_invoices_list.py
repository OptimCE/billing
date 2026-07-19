"""Admin invoice listing: GET /invoices.

Covers the manager/admin-facing list (all invoices in the community) and its
filters: status, participant (id_member), sort/order, and the issued-date range.
Auth + fake-publisher helpers are reused from test_my_invoices; seeding here
gives the two members DISTINCT totals so ordering is unambiguous.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import main
from api.billing.deps import get_event_publisher
from tests.billing.test_my_invoices import _AUTH, _admin_headers, _FakePublisher
from tests.factories import crm_billing_factory as f
from worker import persistence


async def _seed(client, db_session) -> tuple[int, int, int]:
    """Community with Alice (20 kWh) and Bob (40 kWh) → one DRAFT invoice each, distinct totals."""
    main.app.dependency_overrides[get_event_publisher] = lambda: _FakePublisher()
    cid = await f.create_community(db_session, auth_community_id=_AUTH)
    await f.create_subscription(db_session, id_community=cid, feature="billing", is_active=True)
    op = await f.create_sharing_operation(db_session, id_community=cid)

    members: dict[str, int] = {}
    for ean, name, kwh in (("EAN-LA", "Alice", 20.0), ("EAN-LB", "Bob", 40.0)):
        member = await f.create_member(db_session, id_community=cid, name=name)
        members[name] = member
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
            shared=kwh,
        )

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
    return cid, members["Alice"], members["Bob"]


async def test_list_returns_all_community_invoices(client, db_session):
    _cid, alice, bob = await _seed(client, db_session)

    resp = await client.get("/invoices", headers=_admin_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pagination"]["total"] == 2
    assert {row["id_member"] for row in body["data"]} == {alice, bob}


async def test_list_participant_filter(client, db_session):
    _cid, alice, _bob = await _seed(client, db_session)

    resp = await client.get(f"/invoices?participant={alice}", headers=_admin_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pagination"]["total"] == 1
    assert len(body["data"]) == 1
    assert body["data"][0]["id_member"] == alice


async def test_list_status_filter(client, db_session):
    await _seed(client, db_session)

    # Both invoices start DRAFT (status 0); none are ISSUED (status 1).
    resp = await client.get("/invoices?status=0", headers=_admin_headers())
    assert resp.json()["pagination"]["total"] == 2
    resp = await client.get("/invoices?status=1", headers=_admin_headers())
    body = resp.json()
    assert body["pagination"]["total"] == 0
    assert body["data"] == []


async def test_list_sort_by_total(client, db_session):
    await _seed(client, db_session)

    asc = await client.get("/invoices?sort=total&order=asc", headers=_admin_headers())
    totals_asc = [Decimal(r["total"]) for r in asc.json()["data"]]
    assert len(totals_asc) == 2
    assert totals_asc == sorted(totals_asc)

    desc = await client.get("/invoices?sort=total&order=desc", headers=_admin_headers())
    totals_desc = [Decimal(r["total"]) for r in desc.json()["data"]]
    assert totals_desc == sorted(totals_desc, reverse=True)
    assert totals_asc == list(reversed(totals_desc))


async def test_list_issued_date_range(client, db_session):
    _cid, alice, _bob = await _seed(client, db_session)

    # Issue Alice's invoice so it has an issued_at; Bob's stays DRAFT (issued_at NULL).
    invoice_id = (
        await client.get(f"/invoices?participant={alice}", headers=_admin_headers())
    ).json()["data"][0]["id"]
    resp = await client.post(f"/invoices/{invoice_id}/issue", headers=_admin_headers())
    assert resp.status_code == 200, resp.text

    issued_at = (
        await client.get(f"/invoices?participant={alice}", headers=_admin_headers())
    ).json()["data"][0]["issued_at"]
    issued_date = datetime.datetime.fromisoformat(issued_at).date()
    day_after = issued_date + datetime.timedelta(days=1)

    # issued_from includes the issue day and excludes the still-NULL DRAFT (Bob).
    resp = await client.get(
        f"/invoices?issued_from={issued_date.isoformat()}", headers=_admin_headers()
    )
    body = resp.json()
    assert body["pagination"]["total"] == 1
    assert body["data"][0]["id_member"] == alice

    # The day after the issue date excludes everything.
    resp = await client.get(
        f"/invoices?issued_from={day_after.isoformat()}", headers=_admin_headers()
    )
    assert resp.json()["pagination"]["total"] == 0
