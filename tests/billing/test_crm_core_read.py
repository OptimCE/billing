"""Integration tests for the CRM core read port (real Postgres, seeded CRM tables)."""

from __future__ import annotations

import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import text

from ports.crm_core_sqlalchemy import SqlAlchemyCrmCoreRead
from tests.factories import crm_billing_factory as f

_BRUSSELS = ZoneInfo("Europe/Brussels")


async def test_aggregate_by_ean_sums_and_counts(db_session):
    cid = await f.create_community(db_session)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    await f.create_meter(db_session, ean="EAN-A", id_community=cid)
    await f.create_meter(db_session, ean="EAN-B", id_community=cid)

    for day, shared in [(5, 1.0), (10, 2.0), (15, 3.0)]:
        await f.create_meter_consumption(
            db_session,
            ean="EAN-A",
            id_community=cid,
            id_sharing_operation=op,
            timestamp=f.june(day),
            shared=shared,
        )
    await f.create_meter_consumption(
        db_session,
        ean="EAN-B",
        id_community=cid,
        id_sharing_operation=op,
        timestamp=f.june(7),
        inj_shared=5.0,
    )
    # Outside the June period → excluded.
    await f.create_meter_consumption(
        db_session,
        ean="EAN-A",
        id_community=cid,
        id_sharing_operation=op,
        timestamp=datetime.datetime(2026, 7, 2, 12, tzinfo=_BRUSSELS),
        shared=99.0,
    )

    port = SqlAlchemyCrmCoreRead(db_session)
    start, end = f.seed_period()
    aggs = {
        a.ean: a
        for a in await port.aggregate_by_ean(
            id_community=cid, id_sharing_operation=op, period_start=start, period_end=end
        )
    }

    assert aggs["EAN-A"].shared_sum == Decimal("6.0")
    assert aggs["EAN-A"].inj_shared_sum == Decimal("0")
    assert aggs["EAN-A"].row_count == 3
    assert aggs["EAN-A"].distinct_ts == 3
    assert aggs["EAN-A"].has_duplicate_rows is False
    assert aggs["EAN-B"].inj_shared_sum == Decimal("5.0")


async def test_duplicate_rows_flagged(db_session):
    cid = await f.create_community(db_session)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    await f.create_meter(db_session, ean="EAN-D", id_community=cid)
    ts = f.june(9)
    await f.create_meter_consumption(
        db_session, ean="EAN-D", id_community=cid, id_sharing_operation=op, timestamp=ts, shared=1.0
    )
    await f.create_meter_consumption(
        db_session, ean="EAN-D", id_community=cid, id_sharing_operation=op, timestamp=ts, shared=1.0
    )

    port = SqlAlchemyCrmCoreRead(db_session)
    start, end = f.seed_period()
    agg = (
        await port.aggregate_by_ean(
            id_community=cid, id_sharing_operation=op, period_start=start, period_end=end
        )
    )[0]
    assert agg.row_count == 2
    assert agg.distinct_ts == 1
    assert agg.has_duplicate_rows is True


async def test_consumption_exists(db_session):
    cid = await f.create_community(db_session)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    port = SqlAlchemyCrmCoreRead(db_session)
    start, end = f.seed_period()

    assert (
        await port.consumption_exists(
            id_community=cid, id_sharing_operation=op, period_start=start, period_end=end
        )
        is False
    )

    await f.create_meter(db_session, ean="EAN-X", id_community=cid)
    await f.create_meter_consumption(
        db_session,
        ean="EAN-X",
        id_community=cid,
        id_sharing_operation=op,
        timestamp=f.june(3),
        shared=1.0,
    )
    assert (
        await port.consumption_exists(
            id_community=cid, id_sharing_operation=op, period_start=start, period_end=end
        )
        is True
    )


async def test_active_eans_respects_window(db_session):
    cid = await f.create_community(db_session)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    members = [await f.create_member(db_session, id_community=cid) for _ in range(3)]
    for ean in ("EAN-IN", "EAN-ENDED", "EAN-FUTURE"):
        await f.create_meter(db_session, ean=ean, id_community=cid)

    await f.create_meter_data(
        db_session,
        ean="EAN-IN",
        id_community=cid,
        id_sharing_operation=op,
        id_member=members[0],
        client_type=2,
        start_date=datetime.date(2026, 1, 1),
        end_date=None,
    )
    await f.create_meter_data(
        db_session,
        ean="EAN-ENDED",
        id_community=cid,
        id_sharing_operation=op,
        id_member=members[1],
        start_date=datetime.date(2026, 1, 1),
        end_date=datetime.date(2026, 5, 31),
    )
    await f.create_meter_data(
        db_session,
        ean="EAN-FUTURE",
        id_community=cid,
        id_sharing_operation=op,
        id_member=members[2],
        start_date=datetime.date(2026, 7, 1),
        end_date=None,
    )

    port = SqlAlchemyCrmCoreRead(db_session)
    start, end = f.seed_period()
    active = await port.active_eans(
        id_community=cid, id_sharing_operation=op, period_start=start, period_end=end
    )
    by_ean = {a.ean: a for a in active}
    assert set(by_ean) == {"EAN-IN"}
    assert by_ean["EAN-IN"].id_member == members[0]
    assert by_ean["EAN-IN"].client_type == 2


async def test_get_community_identity(db_session):
    cid = await f.create_community(
        db_session, iban="BE68539007547034", legal_name="ACME ASBL", account_holder_name=None
    )
    addr = await f.create_address(
        db_session, id_community=cid, street="Rue de la Loi", number=16, city="Bruxelles"
    )
    await db_session.execute(
        text("UPDATE community SET headquarters_address_id = :a WHERE id = :c"),
        {"a": addr, "c": cid},
    )

    port = SqlAlchemyCrmCoreRead(db_session)
    identity = await port.get_community_identity(id_community=cid)
    assert identity is not None
    assert identity.iban == "BE68539007547034"
    assert identity.legal_name == "ACME ASBL"
    assert identity.street == "Rue de la Loi"
    # CRM stores the house number as an integer; the adapter coerces it to the
    # DTO's declared `str | None` so the docgen address formatter never sees an int.
    assert identity.number == "16"
    assert identity.city == "Bruxelles"
    assert identity.has_billing_info is True


async def test_community_identity_missing_iban_is_incomplete(db_session):
    cid = await f.create_community(db_session, iban=None)
    port = SqlAlchemyCrmCoreRead(db_session)
    identity = await port.get_community_identity(id_community=cid)
    assert identity is not None
    assert identity.has_billing_info is False


async def test_participant_contacts_individual_and_company(db_session):
    cid = await f.create_community(db_session)
    billing_addr = await f.create_address(
        db_session, id_community=cid, street="Chaussée de Liège", number=5, city="Namur"
    )
    m_ind = await f.create_member(
        db_session,
        id_community=cid,
        name="Alice Dupont",
        member_type=1,
        id_billing_address=billing_addr,
    )
    await f.create_individual(
        db_session, id_member=m_ind, email="alice@example.be", social_rate=True
    )

    mgr = await f.create_manager(db_session, id_community=cid, email="manager@corp.be")
    m_co = await f.create_member(db_session, id_community=cid, name="Corp SRL", member_type=2)
    await f.create_company(db_session, id_member=m_co, vat_number="BE0999888777", id_manager=mgr)

    port = SqlAlchemyCrmCoreRead(db_session)
    contacts = await port.participant_contacts(id_community=cid, member_ids=[m_ind, m_co])

    assert contacts[m_ind].email == "alice@example.be"
    assert contacts[m_ind].social_rate is True
    assert contacts[m_ind].number == "5"  # int CRM column coerced to str by the adapter
    assert contacts[m_ind].city == "Namur"
    assert contacts[m_ind].member_type == 1

    assert contacts[m_co].email == "manager@corp.be"  # resolved via company → manager
    assert contacts[m_co].vat_number == "BE0999888777"
    assert contacts[m_co].social_rate is False
    assert contacts[m_co].member_type == 2


async def test_participant_contacts_empty_returns_empty(db_session):
    port = SqlAlchemyCrmCoreRead(db_session)
    assert await port.participant_contacts(id_community=1, member_ids=[]) == {}
