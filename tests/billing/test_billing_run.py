"""End-to-end billing-run tests: API pre-flight + snapshot, worker pricing."""

from __future__ import annotations

import datetime
from decimal import Decimal

import main
from api.billing.deps import get_event_publisher
from tests.factories import crm_billing_factory as f
from worker import persistence

_AUTH = "billing-test-org"


def _headers() -> dict[str, str]:
    return {
        "x-user-id": "user-1",
        "x-community-id": _AUTH,
        "x-user-orgs": f"[orgId:{_AUTH} orgPath:/x roles:[ADMIN]]",
    }


class _FakePublisher:
    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, subject: str, event) -> None:
        self.events.append((subject, event))


def _use_fake_publisher() -> _FakePublisher:
    fake = _FakePublisher()
    main.app.dependency_overrides[get_event_publisher] = lambda: fake
    return fake


async def _seed_operation(db_session, *, iban: str | None = "BE68539007547034") -> tuple[int, int]:
    cid = await f.create_community(
        db_session, auth_community_id=_AUTH, iban=iban, legal_name="ACME ASBL"
    )
    await f.create_subscription(db_session, id_community=cid, feature="billing", is_active=True)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    return cid, op


async def _seed_consumption(db_session, cid: int, op: int) -> None:
    consumer = await f.create_member(db_session, id_community=cid, name="Consumer", member_type=1)
    producer = await f.create_member(db_session, id_community=cid, name="Producer", member_type=1)
    await f.create_meter(db_session, ean="EAN-C", id_community=cid)
    await f.create_meter(db_session, ean="EAN-P", id_community=cid)
    await f.create_meter_data(
        db_session,
        ean="EAN-C",
        id_community=cid,
        id_sharing_operation=op,
        id_member=consumer,
        client_type=1,
        start_date=datetime.date(2026, 1, 1),
    )
    await f.create_meter_data(
        db_session,
        ean="EAN-P",
        id_community=cid,
        id_sharing_operation=op,
        id_member=producer,
        client_type=1,
        start_date=datetime.date(2026, 1, 1),
    )
    await f.create_meter_consumption(
        db_session,
        ean="EAN-C",
        id_community=cid,
        id_sharing_operation=op,
        timestamp=f.june(5),
        shared=10.0,
    )
    await f.create_meter_consumption(
        db_session,
        ean="EAN-C",
        id_community=cid,
        id_sharing_operation=op,
        timestamp=f.june(10),
        shared=20.0,
    )
    await f.create_meter_consumption(
        db_session,
        ean="EAN-P",
        id_community=cid,
        id_sharing_operation=op,
        timestamp=f.june(7),
        inj_shared=15.0,
    )


async def _create_global_tariffs(client, op: int) -> None:
    for kind, price in ((1, "0.15"), (2, "0.08")):
        resp = await client.post(
            f"/sharing-operations/{op}/tariffs",
            headers=_headers(),
            json={"kind": kind, "scope": 1, "price_per_kwh": price, "valid_from": "2026-01-01"},
        )
        assert resp.status_code == 200, resp.text


async def test_billing_run_end_to_end(client, db_session):
    _use_fake_publisher()
    cid, op = await _seed_operation(db_session)
    await _seed_consumption(db_session, cid, op)
    await _create_global_tariffs(client, op)

    resp = await client.post(
        f"/sharing-operations/{op}/billing-runs",
        headers=_headers(),
        json={"period_start": "2026-06-01", "period_end": "2026-06-30"},
    )
    assert resp.status_code == 200, resp.text
    run = resp.json()["data"]
    assert run["status"] == 0  # PENDING
    run_id = run["id"]

    count = await persistence.process_billing_run(
        run_id, local_session=db_session, crm_session=db_session
    )
    assert count == 2

    resp = await client.get(f"/billing-runs/{run_id}/invoices", headers=_headers())
    assert resp.status_code == 200
    by_type = {inv["type"]: inv for inv in resp.json()["data"]}

    consumer_invoice = by_type[1]  # INVOICE
    assert Decimal(consumer_invoice["subtotal"]) == Decimal("4.50")  # 30 kWh x 0.15
    assert Decimal(consumer_invoice["vat_amount"]) == Decimal("0.95")
    assert Decimal(consumer_invoice["total"]) == Decimal("5.45")
    assert consumer_invoice["status"] == 0  # DRAFT
    assert consumer_invoice["number"] is None  # not issued yet

    producer_statement = by_type[3]  # PRODUCER_STATEMENT
    assert Decimal(producer_statement["subtotal"]) == Decimal("1.20")  # 15 kWh x 0.08

    resp = await client.get(f"/billing-runs/{run_id}", headers=_headers())
    detail = resp.json()["data"]
    assert detail["status"] == 2  # COMPUTED
    assert detail["invoice_count"] == 2

    # Idempotent re-processing produces no duplicates.
    count_again = await persistence.process_billing_run(
        run_id, local_session=db_session, crm_session=db_session
    )
    assert count_again == 2
    resp = await client.get(f"/billing-runs/{run_id}/invoices", headers=_headers())
    assert len(resp.json()["data"]) == 2


async def test_billing_run_mid_month_transfer_prorates_by_owner(client, db_session):
    """A meter changing owner mid-month yields one invoice per owner, each
    billed exactly for the volume read during their ownership window."""
    _use_fake_publisher()
    cid, op = await _seed_operation(db_session)
    m_a = await f.create_member(db_session, id_community=cid, name="First Owner")
    m_b = await f.create_member(db_session, id_community=cid, name="Second Owner")
    await f.create_meter(db_session, ean="EAN-T", id_community=cid)
    await f.create_meter_data(
        db_session,
        ean="EAN-T",
        id_community=cid,
        id_sharing_operation=op,
        id_member=m_a,
        client_type=1,
        start_date=datetime.date(2026, 6, 1),
        end_date=datetime.date(2026, 6, 15),
    )
    await f.create_meter_data(
        db_session,
        ean="EAN-T",
        id_community=cid,
        id_sharing_operation=op,
        id_member=m_b,
        client_type=1,
        start_date=datetime.date(2026, 6, 16),
        end_date=None,
    )
    for ts, shared in [(f.june(5), 10.0), (f.june(10), 20.0), (f.june(20), 40.0)]:
        await f.create_meter_consumption(
            db_session,
            ean="EAN-T",
            id_community=cid,
            id_sharing_operation=op,
            timestamp=ts,
            shared=shared,
        )
    await _create_global_tariffs(client, op)

    resp = await client.post(
        f"/sharing-operations/{op}/billing-runs",
        headers=_headers(),
        json={"period_start": "2026-06-01", "period_end": "2026-06-30"},
    )
    assert resp.status_code == 200, resp.text
    run = resp.json()["data"]
    assert run["warnings"] is None
    run_id = run["id"]

    count = await persistence.process_billing_run(
        run_id, local_session=db_session, crm_session=db_session
    )
    assert count == 2  # one consumer invoice per owner

    resp = await client.get(f"/billing-runs/{run_id}/invoices", headers=_headers())
    invoices = {inv["id_member"]: inv for inv in resp.json()["data"]}
    assert set(invoices) == {m_a, m_b}
    assert Decimal(invoices[m_a]["subtotal"]) == Decimal("4.50")  # 30 kWh x 0.15
    assert Decimal(invoices[m_b]["subtotal"]) == Decimal("6.00")  # 40 kWh x 0.15

    # Each invoice line carries the owner's window so the split is auditable.
    detail_a = (await client.get(f"/invoices/{invoices[m_a]['id']}", headers=_headers())).json()
    detail_b = (await client.get(f"/invoices/{invoices[m_b]['id']}", headers=_headers())).json()
    assert "du 01/06/2026 au 15/06/2026" in detail_a["data"]["lines"][0]["description"]
    assert "du 16/06/2026 au 30/06/2026" in detail_b["data"]["lines"][0]["description"]


async def test_billing_run_refuses_overlapping_ownership(client, db_session):
    _use_fake_publisher()
    cid, op = await _seed_operation(db_session)
    m_a = await f.create_member(db_session, id_community=cid)
    m_b = await f.create_member(db_session, id_community=cid)
    await f.create_meter(db_session, ean="EAN-OVL", id_community=cid)
    await f.create_meter_data(
        db_session,
        ean="EAN-OVL",
        id_community=cid,
        id_sharing_operation=op,
        id_member=m_a,
        start_date=datetime.date(2026, 6, 1),
        end_date=datetime.date(2026, 6, 20),
    )
    await f.create_meter_data(
        db_session,
        ean="EAN-OVL",
        id_community=cid,
        id_sharing_operation=op,
        id_member=m_b,
        start_date=datetime.date(2026, 6, 15),
        end_date=None,
    )
    await f.create_meter_consumption(
        db_session,
        ean="EAN-OVL",
        id_community=cid,
        id_sharing_operation=op,
        timestamp=f.june(17),
        shared=10.0,
    )

    resp = await client.post(
        f"/sharing-operations/{op}/billing-runs",
        headers=_headers(),
        json={"period_start": "2026-06-01", "period_end": "2026-06-30"},
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == 2217  # METER_OWNERSHIP_OVERLAP

    # The refused run left nothing behind: same period can be retried later.
    resp = await client.get(f"/sharing-operations/{op}/billing-runs", headers=_headers())
    assert resp.json()["data"] == []


async def test_billing_run_orphan_volume_warns_and_stays_unbilled(client, db_session):
    _use_fake_publisher()
    cid, op = await _seed_operation(db_session)
    m = await f.create_member(db_session, id_community=cid)
    await f.create_meter(db_session, ean="EAN-O", id_community=cid)
    # Ownership only starts on the 16th: earlier readings belong to nobody.
    await f.create_meter_data(
        db_session,
        ean="EAN-O",
        id_community=cid,
        id_sharing_operation=op,
        id_member=m,
        client_type=1,
        start_date=datetime.date(2026, 6, 16),
    )
    await f.create_meter_consumption(
        db_session,
        ean="EAN-O",
        id_community=cid,
        id_sharing_operation=op,
        timestamp=f.june(5),
        shared=10.0,
    )
    await f.create_meter_consumption(
        db_session,
        ean="EAN-O",
        id_community=cid,
        id_sharing_operation=op,
        timestamp=f.june(20),
        shared=20.0,
    )
    await _create_global_tariffs(client, op)

    resp = await client.post(
        f"/sharing-operations/{op}/billing-runs",
        headers=_headers(),
        json={"period_start": "2026-06-01", "period_end": "2026-06-30"},
    )
    assert resp.status_code == 200, resp.text
    run = resp.json()["data"]
    assert run["warnings"] is not None
    assert run["warnings"][0]["code"] == "ORPHAN_VOLUME"
    assert run["warnings"][0]["eans"][0]["ean"] == "EAN-O"
    assert Decimal(run["warnings"][0]["eans"][0]["shared_kwh"]) == Decimal("10.0")
    run_id = run["id"]

    count = await persistence.process_billing_run(
        run_id, local_session=db_session, crm_session=db_session
    )
    assert count == 1  # only the owned volume is invoiced

    resp = await client.get(f"/billing-runs/{run_id}/invoices", headers=_headers())
    invoices = resp.json()["data"]
    assert len(invoices) == 1
    assert invoices[0]["id_member"] == m
    assert Decimal(invoices[0]["subtotal"]) == Decimal("3.00")  # 20 kWh x 0.15


async def test_run_requires_community_iban(client, db_session):
    _use_fake_publisher()
    cid, op = await _seed_operation(db_session, iban=None)
    await _seed_consumption(db_session, cid, op)
    resp = await client.post(
        f"/sharing-operations/{op}/billing-runs",
        headers=_headers(),
        json={"period_start": "2026-06-01", "period_end": "2026-06-30"},
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == 2214  # COMMUNITY_BILLING_INFO_INCOMPLETE


async def test_run_requires_consumption(client, db_session):
    _use_fake_publisher()
    _cid, op = await _seed_operation(db_session)
    resp = await client.post(
        f"/sharing-operations/{op}/billing-runs",
        headers=_headers(),
        json={"period_start": "2026-06-01", "period_end": "2026-06-30"},
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == 2213  # NO_CONSUMPTION_DATA


async def test_run_requires_tariff(client, db_session):
    _use_fake_publisher()
    cid, op = await _seed_operation(db_session)
    await _seed_consumption(db_session, cid, op)  # consumption but no tariffs
    resp = await client.post(
        f"/sharing-operations/{op}/billing-runs",
        headers=_headers(),
        json={"period_start": "2026-06-01", "period_end": "2026-06-30"},
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == 2201  # TARIFF_NOT_FOUND
