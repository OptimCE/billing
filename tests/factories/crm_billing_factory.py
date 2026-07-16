"""Async seeding helpers for the CRM core tables the billing service reads.

These INSERT into the test Postgres (where tests/sql/crm_test_schema.sql is
applied) using raw SQL and return generated ids. Everything runs inside the
per-test rolled-back transaction, so no cleanup is needed.
"""

from __future__ import annotations

import datetime
import itertools
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_seq = itertools.count(1)


async def create_community(
    session: AsyncSession,
    *,
    regulator: str = "BE-WAL-CWAPE",
    legal_name: str | None = "ACME Energy Community ASBL",
    vat_number: str | None = "BE0123456789",
    iban: str | None = "BE68539007547034",
    account_holder_name: str | None = None,
    headquarters_address_id: int | None = None,
    name: str | None = None,
    auth_community_id: str | None = None,
) -> int:
    n = next(_seq)
    result = await session.execute(
        text(
            """
            INSERT INTO community (name, auth_community_id, regulator, vat_number,
                                   legal_name, iban, account_holder_name, headquarters_address_id)
            VALUES (:name, :auth, :regulator, :vat, :legal, :iban, :ahn, :hq)
            RETURNING id
            """
        ),
        {
            "name": name or f"Community {n}",
            "auth": auth_community_id or f"auth-community-{n}",
            "regulator": regulator,
            "vat": vat_number,
            "legal": legal_name,
            "iban": iban,
            "ahn": account_holder_name,
            "hq": headquarters_address_id,
        },
    )
    return int(result.scalar_one())


async def create_subscription(
    session: AsyncSession, *, id_community: int, feature: str = "billing", is_active: bool = True
) -> int:
    result = await session.execute(
        text(
            """
            INSERT INTO community_subscription (id_community, feature, is_active)
            VALUES (:cid, :feature, :active)
            RETURNING id
            """
        ),
        {"cid": id_community, "feature": feature, "active": is_active},
    )
    return int(result.scalar_one())


async def create_address(
    session: AsyncSession,
    *,
    id_community: int,
    street: str = "Rue de la Loi",
    number: int = 16,  # integer column in the real CRM (see crm_test_schema.sql)
    postcode: str = "1000",
    city: str = "Bruxelles",
    supplement: str | None = None,
) -> int:
    result = await session.execute(
        text(
            """
            INSERT INTO address (street, number, postcode, city, supplement, id_community)
            VALUES (:street, :number, :postcode, :city, :supplement, :cid)
            RETURNING id
            """
        ),
        {
            "street": street,
            "number": number,
            "postcode": postcode,
            "city": city,
            "supplement": supplement,
            "cid": id_community,
        },
    )
    return int(result.scalar_one())


async def create_sharing_operation(
    session: AsyncSession, *, id_community: int, name: str | None = None
) -> int:
    n = next(_seq)
    result = await session.execute(
        text(
            """
            INSERT INTO sharing_operation (name, type, is_public, id_community)
            VALUES (:name, 1, FALSE, :cid)
            RETURNING id
            """
        ),
        {"name": name or f"Operation {n}", "cid": id_community},
    )
    return int(result.scalar_one())


async def create_meter(session: AsyncSession, *, ean: str, id_community: int) -> str:
    await session.execute(
        text(
            """
            INSERT INTO meter (ean, meter_number, id_community)
            VALUES (:ean, :mn, :cid)
            """
        ),
        {"ean": ean, "mn": f"MTR-{ean[-6:]}", "cid": id_community},
    )
    return ean


async def create_meter_data(
    session: AsyncSession,
    *,
    ean: str,
    id_community: int,
    id_sharing_operation: int,
    id_member: int | None = None,
    status: int = 1,
    client_type: int = 1,
    injection_status: int | None = None,
    start_date: datetime.date,
    end_date: datetime.date | None = None,
) -> int:
    result = await session.execute(
        text(
            """
            INSERT INTO meter_data (ean, id_member, id_sharing_operation, status, client_type,
                                    injection_status, start_date, end_date, id_community)
            VALUES (:ean, :mem, :op, :status, :ct, :inj, :start, :end, :cid)
            RETURNING id
            """
        ),
        {
            "ean": ean,
            "mem": id_member,
            "op": id_sharing_operation,
            "status": status,
            "ct": client_type,
            "inj": injection_status,
            "start": start_date,
            "end": end_date,
            "cid": id_community,
        },
    )
    return int(result.scalar_one())


async def create_meter_consumption(
    session: AsyncSession,
    *,
    ean: str,
    id_community: int,
    id_sharing_operation: int,
    timestamp: datetime.datetime,
    shared: float = 0.0,
    inj_shared: float = 0.0,
    gross: float = 0.0,
    net: float = 0.0,
    inj_gross: float = 0.0,
    inj_net: float = 0.0,
) -> int:
    result = await session.execute(
        text(
            """
            INSERT INTO meter_consumption (ean, id_sharing_operation, timestamp, gross, net, shared,
                                           inj_gross, inj_shared, inj_net, id_community)
            VALUES (:ean, :op, :ts, :gross, :net, :shared, :inj_gross, :inj_shared, :inj_net, :cid)
            RETURNING id
            """
        ),
        {
            "ean": ean,
            "op": id_sharing_operation,
            "ts": timestamp,
            "gross": gross,
            "net": net,
            "shared": shared,
            "inj_gross": inj_gross,
            "inj_shared": inj_shared,
            "inj_net": inj_net,
            "cid": id_community,
        },
    )
    return int(result.scalar_one())


async def create_manager(
    session: AsyncSession, *, id_community: int, email: str, name: str = "Manager"
) -> int:
    result = await session.execute(
        text(
            """
            INSERT INTO manager (name, email, id_community)
            VALUES (:name, :email, :cid)
            RETURNING id
            """
        ),
        {"name": name, "email": email, "cid": id_community},
    )
    return int(result.scalar_one())


async def create_member(
    session: AsyncSession,
    *,
    id_community: int,
    name: str = "Jane Doe",
    member_type: int = 1,
    status: int = 1,
    iban: str | None = None,
    id_billing_address: int | None = None,
    id_home_address: int | None = None,
) -> int:
    result = await session.execute(
        text(
            """
            INSERT INTO member (name, member_type, status, iban, id_home_address,
                                id_billing_address, id_community)
            VALUES (:name, :mt, :status, :iban, :home, :billing, :cid)
            RETURNING id
            """
        ),
        {
            "name": name,
            "mt": member_type,
            "status": status,
            "iban": iban,
            "home": id_home_address,
            "billing": id_billing_address,
            "cid": id_community,
        },
    )
    return int(result.scalar_one())


async def create_individual(
    session: AsyncSession,
    *,
    id_member: int,
    first_name: str = "Jane",
    email: str | None = "jane@example.be",
    social_rate: bool = False,
    id_manager: int | None = None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO individual (id, first_name, email, social_rate, id_manager)
            VALUES (:id, :fn, :email, :sr, :mgr)
            """
        ),
        {"id": id_member, "fn": first_name, "email": email, "sr": social_rate, "mgr": id_manager},
    )


async def create_company(
    session: AsyncSession,
    *,
    id_member: int,
    vat_number: str = "BE0987654321",
    id_manager: int | None = None,
) -> None:
    await session.execute(
        text("INSERT INTO company (id, vat_number, id_manager) VALUES (:id, :vat, :mgr)"),
        {"id": id_member, "vat": vat_number, "mgr": id_manager},
    )


async def create_app_user(
    session: AsyncSession, *, auth_user_id: str, email: str | None = None
) -> int:
    """A CRM app_user (the Keycloak identity); returns its id for user_member_link."""
    n = next(_seq)
    result = await session.execute(
        text("INSERT INTO app_user (auth_user_id, email) VALUES (:auth, :email) RETURNING id"),
        {"auth": auth_user_id, "email": email or f"user-{n}@example.be"},
    )
    return int(result.scalar_one())


async def link_user_to_member(session: AsyncSession, *, id_user: int, id_member: int) -> None:
    await session.execute(
        text("INSERT INTO user_member_link (id_user, id_member) VALUES (:u, :m)"),
        {"u": id_user, "m": id_member},
    )


def june(day: int, hour: int = 12) -> datetime.datetime:
    """A tz-aware June 2026 instant (Brussels), for consumption timestamps."""
    from zoneinfo import ZoneInfo

    return datetime.datetime(2026, 6, day, hour, tzinfo=ZoneInfo("Europe/Brussels"))


def seed_period() -> tuple[datetime.date, datetime.date]:
    """The inclusive June-2026 billing period used across tests."""
    return datetime.date(2026, 6, 1), datetime.date(2026, 6, 30)


__all__: Sequence[str] = [
    "create_address",
    "create_app_user",
    "create_community",
    "create_company",
    "create_individual",
    "create_manager",
    "create_member",
    "create_meter",
    "create_meter_consumption",
    "create_meter_data",
    "create_sharing_operation",
    "create_subscription",
    "june",
    "link_user_to_member",
    "seed_period",
]
