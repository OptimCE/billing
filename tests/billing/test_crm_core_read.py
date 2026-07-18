"""Integration tests for the CRM core read port (real Postgres, seeded CRM tables)."""

from __future__ import annotations

import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import text

from ports.crm_core_sqlalchemy import SqlAlchemyCrmCoreRead
from tests.factories import crm_billing_factory as f

_BRUSSELS = ZoneInfo("Europe/Brussels")


async def _aggregate(port, cid, op):
    start, end = f.seed_period()
    return await port.aggregate_by_ean_member(
        id_community=cid, id_sharing_operation=op, period_start=start, period_end=end
    )


async def test_aggregate_by_ean_member_sums_and_counts(db_session):
    cid = await f.create_community(db_session)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    m_a = await f.create_member(db_session, id_community=cid)
    m_b = await f.create_member(db_session, id_community=cid)
    await f.create_meter(db_session, ean="EAN-A", id_community=cid)
    await f.create_meter(db_session, ean="EAN-B", id_community=cid)
    await f.create_meter_data(
        db_session, ean="EAN-A", id_community=cid, id_sharing_operation=op,
        id_member=m_a, client_type=2, start_date=datetime.date(2026, 1, 1),
    )
    await f.create_meter_data(
        db_session, ean="EAN-B", id_community=cid, id_sharing_operation=op,
        id_member=m_b, start_date=datetime.date(2026, 1, 1),
    )

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
    aggs = {(a.ean, a.id_member): a for a in await _aggregate(port, cid, op)}

    agg_a = aggs[("EAN-A", m_a)]
    assert agg_a.shared_sum == Decimal("6.0")
    assert agg_a.inj_shared_sum == Decimal("0")
    assert agg_a.row_count == 3
    assert agg_a.distinct_ts == 3
    assert agg_a.has_duplicate_rows is False
    assert agg_a.client_type == 2
    assert agg_a.owned_from == datetime.date(2026, 1, 1)
    assert agg_a.owned_to is None  # open-ended window
    assert aggs[("EAN-B", m_b)].inj_shared_sum == Decimal("5.0")


async def test_aggregate_mid_month_transfer_splits_by_owner(db_session):
    cid = await f.create_community(db_session)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    m_a = await f.create_member(db_session, id_community=cid, name="A")
    m_b = await f.create_member(db_session, id_community=cid, name="B")
    await f.create_meter(db_session, ean="EAN-T", id_community=cid)
    await f.create_meter_data(
        db_session, ean="EAN-T", id_community=cid, id_sharing_operation=op,
        id_member=m_a, client_type=1,
        start_date=datetime.date(2026, 6, 1), end_date=datetime.date(2026, 6, 15),
    )
    await f.create_meter_data(
        db_session, ean="EAN-T", id_community=cid, id_sharing_operation=op,
        id_member=m_b, client_type=2,
        start_date=datetime.date(2026, 6, 16), end_date=None,
    )

    for ts, shared in [
        (f.june(5), 1.0),
        (f.june(10), 2.0),
        (f.june(15, hour=23), 4.0),  # last hour of A's window
        # 22:30 UTC on the 15th is already June 16 00:30 in Brussels → B's day.
        (datetime.datetime(2026, 6, 15, 22, 30, tzinfo=datetime.UTC), 32.0),
        (f.june(16, hour=0), 8.0),  # first instant of B's window
        (f.june(20), 16.0),
    ]:
        await f.create_meter_consumption(
            db_session, ean="EAN-T", id_community=cid, id_sharing_operation=op,
            timestamp=ts, shared=shared,
        )

    port = SqlAlchemyCrmCoreRead(db_session)
    aggs = {a.id_member: a for a in await _aggregate(port, cid, op)}
    assert set(aggs) == {m_a, m_b}

    assert aggs[m_a].shared_sum == Decimal("7.0")  # 1 + 2 + 4
    assert aggs[m_a].row_count == 3
    assert aggs[m_a].client_type == 1
    assert aggs[m_a].owned_from == datetime.date(2026, 6, 1)
    assert aggs[m_a].owned_to == datetime.date(2026, 6, 15)

    assert aggs[m_b].shared_sum == Decimal("56.0")  # 32 + 8 + 16
    assert aggs[m_b].row_count == 3
    assert aggs[m_b].client_type == 2
    assert aggs[m_b].owned_from == datetime.date(2026, 6, 16)
    assert aggs[m_b].owned_to is None


async def test_aggregate_orphan_volume_grouped_under_null_member(db_session):
    cid = await f.create_community(db_session)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    m = await f.create_member(db_session, id_community=cid)
    await f.create_meter(db_session, ean="EAN-O", id_community=cid)
    # Ownership only starts mid-month: earlier readings belong to nobody.
    await f.create_meter_data(
        db_session, ean="EAN-O", id_community=cid, id_sharing_operation=op,
        id_member=m, start_date=datetime.date(2026, 6, 16),
    )
    await f.create_meter_consumption(
        db_session, ean="EAN-O", id_community=cid, id_sharing_operation=op,
        timestamp=f.june(5), shared=3.0,
    )
    await f.create_meter_consumption(
        db_session, ean="EAN-O", id_community=cid, id_sharing_operation=op,
        timestamp=f.june(20), shared=5.0,
    )

    port = SqlAlchemyCrmCoreRead(db_session)
    aggs = {a.id_member: a for a in await _aggregate(port, cid, op)}
    assert set(aggs) == {None, m}
    assert aggs[None].shared_sum == Decimal("3.0")
    assert aggs[None].owned_from is None
    assert aggs[None].client_type is None
    assert aggs[m].shared_sum == Decimal("5.0")


async def test_aggregate_rejoin_merges_single_group_latest_client_type(db_session):
    cid = await f.create_community(db_session)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    m_a = await f.create_member(db_session, id_community=cid, name="A")
    m_b = await f.create_member(db_session, id_community=cid, name="B")
    await f.create_meter(db_session, ean="EAN-R", id_community=cid)
    # A owns, hands over to B, then re-acquires with a different client_type.
    await f.create_meter_data(
        db_session, ean="EAN-R", id_community=cid, id_sharing_operation=op,
        id_member=m_a, client_type=1,
        start_date=datetime.date(2026, 6, 1), end_date=datetime.date(2026, 6, 10),
    )
    await f.create_meter_data(
        db_session, ean="EAN-R", id_community=cid, id_sharing_operation=op,
        id_member=m_b, client_type=1,
        start_date=datetime.date(2026, 6, 11), end_date=datetime.date(2026, 6, 20),
    )
    await f.create_meter_data(
        db_session, ean="EAN-R", id_community=cid, id_sharing_operation=op,
        id_member=m_a, client_type=2,
        start_date=datetime.date(2026, 6, 21), end_date=None,
    )
    for ts, shared in [(f.june(5), 1.0), (f.june(15), 2.0), (f.june(25), 4.0)]:
        await f.create_meter_consumption(
            db_session, ean="EAN-R", id_community=cid, id_sharing_operation=op,
            timestamp=ts, shared=shared,
        )

    port = SqlAlchemyCrmCoreRead(db_session)
    aggs = {a.id_member: a for a in await _aggregate(port, cid, op)}
    assert set(aggs) == {m_a, m_b}
    # A's two windows merge into one aggregate spanning their union.
    assert aggs[m_a].shared_sum == Decimal("5.0")  # 1 + 4
    assert aggs[m_a].client_type == 2  # latest window wins
    assert aggs[m_a].owned_from == datetime.date(2026, 6, 1)
    assert aggs[m_a].owned_to is None  # latest window is open-ended
    assert aggs[m_b].shared_sum == Decimal("2.0")


async def test_duplicate_rows_flagged(db_session):
    cid = await f.create_community(db_session)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    m = await f.create_member(db_session, id_community=cid)
    await f.create_meter(db_session, ean="EAN-D", id_community=cid)
    await f.create_meter_data(
        db_session, ean="EAN-D", id_community=cid, id_sharing_operation=op,
        id_member=m, start_date=datetime.date(2026, 1, 1),
    )
    ts = f.june(9)
    await f.create_meter_consumption(
        db_session, ean="EAN-D", id_community=cid, id_sharing_operation=op, timestamp=ts, shared=1.0
    )
    await f.create_meter_consumption(
        db_session, ean="EAN-D", id_community=cid, id_sharing_operation=op, timestamp=ts, shared=1.0
    )

    port = SqlAlchemyCrmCoreRead(db_session)
    agg = (await _aggregate(port, cid, op))[0]
    assert agg.id_member == m
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


async def test_aggregate_ignores_windows_outside_period(db_session):
    cid = await f.create_community(db_session)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    members = [await f.create_member(db_session, id_community=cid) for _ in range(3)]
    for ean in ("EAN-IN", "EAN-ENDED", "EAN-FUTURE"):
        await f.create_meter(db_session, ean=ean, id_community=cid)
        await f.create_meter_consumption(
            db_session, ean=ean, id_community=cid, id_sharing_operation=op,
            timestamp=f.june(10), shared=1.0,
        )

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
    by_ean = {(a.ean, a.id_member): a for a in await _aggregate(port, cid, op)}
    # Only the in-window membership attributes volume; ended/future ownerships
    # leave their June readings as orphan (NULL-member) groups.
    assert set(by_ean) == {
        ("EAN-IN", members[0]),
        ("EAN-ENDED", None),
        ("EAN-FUTURE", None),
    }
    assert by_ean[("EAN-IN", members[0])].client_type == 2


async def test_find_ownership_overlaps_detects_in_period_overlap(db_session):
    cid = await f.create_community(db_session)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    m_a = await f.create_member(db_session, id_community=cid)
    m_b = await f.create_member(db_session, id_community=cid)
    await f.create_meter(db_session, ean="EAN-OVL", id_community=cid)
    await f.create_meter_data(
        db_session, ean="EAN-OVL", id_community=cid, id_sharing_operation=op,
        id_member=m_a, start_date=datetime.date(2026, 6, 1), end_date=datetime.date(2026, 6, 20),
    )
    await f.create_meter_data(
        db_session, ean="EAN-OVL", id_community=cid, id_sharing_operation=op,
        id_member=m_b, start_date=datetime.date(2026, 6, 15), end_date=None,
    )

    port = SqlAlchemyCrmCoreRead(db_session)
    start, end = f.seed_period()
    assert await port.find_ownership_overlaps(
        id_community=cid, id_sharing_operation=op, period_start=start, period_end=end
    ) == ["EAN-OVL"]


async def test_find_ownership_overlaps_ignores_adjacent_windows(db_session):
    cid = await f.create_community(db_session)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    m_a = await f.create_member(db_session, id_community=cid)
    m_b = await f.create_member(db_session, id_community=cid)
    await f.create_meter(db_session, ean="EAN-ADJ", id_community=cid)
    # end_date = next start_date - 1: a clean hand-over, no overlap.
    await f.create_meter_data(
        db_session, ean="EAN-ADJ", id_community=cid, id_sharing_operation=op,
        id_member=m_a, start_date=datetime.date(2026, 6, 1), end_date=datetime.date(2026, 6, 15),
    )
    await f.create_meter_data(
        db_session, ean="EAN-ADJ", id_community=cid, id_sharing_operation=op,
        id_member=m_b, start_date=datetime.date(2026, 6, 16), end_date=None,
    )

    port = SqlAlchemyCrmCoreRead(db_session)
    start, end = f.seed_period()
    assert (
        await port.find_ownership_overlaps(
            id_community=cid, id_sharing_operation=op, period_start=start, period_end=end
        )
        == []
    )


async def test_find_ownership_overlaps_ignores_out_of_period_and_inactive(db_session):
    cid = await f.create_community(db_session)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    m_a = await f.create_member(db_session, id_community=cid)
    m_b = await f.create_member(db_session, id_community=cid)
    # Overlap fully before June → irrelevant for a June run.
    await f.create_meter(db_session, ean="EAN-PAST", id_community=cid)
    await f.create_meter_data(
        db_session, ean="EAN-PAST", id_community=cid, id_sharing_operation=op,
        id_member=m_a, start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 2, 15),
    )
    await f.create_meter_data(
        db_session, ean="EAN-PAST", id_community=cid, id_sharing_operation=op,
        id_member=m_b, start_date=datetime.date(2026, 2, 10), end_date=datetime.date(2026, 2, 28),
    )
    # In-period overlap but one window is not ACTIVE → ignored.
    await f.create_meter(db_session, ean="EAN-INACT", id_community=cid)
    await f.create_meter_data(
        db_session, ean="EAN-INACT", id_community=cid, id_sharing_operation=op,
        id_member=m_a, start_date=datetime.date(2026, 6, 1), end_date=None,
    )
    await f.create_meter_data(
        db_session, ean="EAN-INACT", id_community=cid, id_sharing_operation=op,
        id_member=m_b, status=2, start_date=datetime.date(2026, 6, 10), end_date=None,
    )

    port = SqlAlchemyCrmCoreRead(db_session)
    start, end = f.seed_period()
    assert (
        await port.find_ownership_overlaps(
            id_community=cid, id_sharing_operation=op, period_start=start, period_end=end
        )
        == []
    )


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
