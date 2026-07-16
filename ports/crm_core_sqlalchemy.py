"""SQLAlchemy adapter for CrmCoreReadPort — the single place coupled to the CRM
core table layout. Every query is SELECT-only and runs on a CRM AsyncSession.

Period boundaries are interpreted in Belgian local time (CWaPE = Wallonia), so a
monthly period aligns to local midnights and is DST-safe. Windows are half-open
[start, end): ``timestamp >= start AND timestamp < end_exclusive``.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from ports.crm_core import ActiveEan, CommunityIdentity, EanAggregate, ParticipantContact

# Settlement timestamps are absolute instants (timestamptz); a period expressed
# as local dates is bounded at Belgian local midnights.
_SETTLEMENT_TZ = ZoneInfo("Europe/Brussels")

# meter_data.status value for an active membership.
_ACTIVE_METER_DATA_STATUS = 1


def _period_bounds(
    period_start: date, period_end: date
) -> tuple[datetime.datetime, datetime.datetime]:
    """Half-open instant bounds [start, end_exclusive) for the inclusive date range."""
    start = datetime.datetime.combine(period_start, datetime.time.min, tzinfo=_SETTLEMENT_TZ)
    end_exclusive = datetime.datetime.combine(
        period_end + datetime.timedelta(days=1), datetime.time.min, tzinfo=_SETTLEMENT_TZ
    )
    return start, end_exclusive


class SqlAlchemyCrmCoreRead:
    """Concrete CrmCoreReadPort over the CRM core database."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def consumption_exists(
        self,
        *,
        id_community: int,
        id_sharing_operation: int,
        period_start: date,
        period_end: date,
    ) -> bool:
        start, end_exclusive = _period_bounds(period_start, period_end)
        result = await self._session.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1 FROM meter_consumption mc
                    WHERE mc.id_community = :cid
                      AND mc.id_sharing_operation = :op
                      AND mc.timestamp >= :start AND mc.timestamp < :end_excl
                ) AS present
                """
            ),
            {
                "cid": id_community,
                "op": id_sharing_operation,
                "start": start,
                "end_excl": end_exclusive,
            },
        )
        return bool(result.scalar())

    async def aggregate_by_ean(
        self,
        *,
        id_community: int,
        id_sharing_operation: int,
        period_start: date,
        period_end: date,
    ) -> list[EanAggregate]:
        start, end_exclusive = _period_bounds(period_start, period_end)
        result = await self._session.execute(
            text(
                """
                SELECT mc.ean                       AS ean,
                       COALESCE(SUM(mc.shared), 0)     AS shared_sum,
                       COALESCE(SUM(mc.inj_shared), 0) AS inj_shared_sum,
                       COUNT(*)                        AS row_count,
                       COUNT(DISTINCT mc.timestamp)    AS distinct_ts
                FROM meter_consumption mc
                WHERE mc.id_community = :cid
                  AND mc.id_sharing_operation = :op
                  AND mc.timestamp >= :start AND mc.timestamp < :end_excl
                GROUP BY mc.ean
                ORDER BY mc.ean
                """
            ),
            {
                "cid": id_community,
                "op": id_sharing_operation,
                "start": start,
                "end_excl": end_exclusive,
            },
        )
        return [
            EanAggregate(
                ean=row["ean"],
                shared_sum=Decimal(str(row["shared_sum"])),
                inj_shared_sum=Decimal(str(row["inj_shared_sum"])),
                row_count=int(row["row_count"]),
                distinct_ts=int(row["distinct_ts"]),
            )
            for row in result.mappings()
        ]

    async def get_community_identity(self, *, id_community: int) -> CommunityIdentity | None:
        result = await self._session.execute(
            text(
                """
                SELECT c.id                  AS id,
                       c.regulator           AS regulator,
                       c.legal_name          AS legal_name,
                       c.vat_number          AS vat_number,
                       c.iban                AS iban,
                       c.account_holder_name AS account_holder_name,
                       a.street              AS street,
                       a.number              AS number,
                       a.postcode            AS postcode,
                       a.city                AS city,
                       a.supplement          AS supplement
                FROM community c
                LEFT JOIN address a ON a.id = c.headquarters_address_id
                WHERE c.id = :cid
                """
            ),
            {"cid": id_community},
        )
        row = result.mappings().first()
        if row is None:
            return None
        data = dict(row)
        # CRM `address.number` is an integer column; the DTO (and the docgen
        # address formatter) treat it as a string. Coerce to honour `str | None`.
        if data.get("number") is not None:
            data["number"] = str(data["number"])
        return CommunityIdentity(**data)

    async def active_eans(
        self,
        *,
        id_community: int,
        id_sharing_operation: int,
        period_start: date,
        period_end: date,
    ) -> list[ActiveEan]:
        end_exclusive_date = period_end + datetime.timedelta(days=1)
        result = await self._session.execute(
            text(
                """
                SELECT md.ean              AS ean,
                       md.id_member        AS id_member,
                       md.client_type      AS client_type,
                       md.injection_status AS injection_status
                FROM meter_data md
                WHERE md.id_community = :cid
                  AND md.id_sharing_operation = :op
                  AND md.status = :active
                  AND md.start_date < :end_excl_date
                  AND (md.end_date IS NULL OR md.end_date >= :start_date)
                ORDER BY md.ean, md.start_date DESC
                """
            ),
            {
                "cid": id_community,
                "op": id_sharing_operation,
                "active": _ACTIVE_METER_DATA_STATUS,
                "start_date": period_start,
                "end_excl_date": end_exclusive_date,
            },
        )
        # One EAN can have several overlapping memberships in a period (e.g. a
        # mid-period change). Keep the most recent (start_date DESC → first seen).
        seen: dict[str, ActiveEan] = {}
        for row in result.mappings():
            ean = row["ean"]
            if ean not in seen:
                seen[ean] = ActiveEan(
                    ean=ean,
                    id_member=row["id_member"],
                    client_type=row["client_type"],
                    injection_status=row["injection_status"],
                )
        return list(seen.values())

    async def participant_contacts(
        self, *, id_community: int, member_ids: Sequence[int]
    ) -> dict[int, ParticipantContact]:
        if not member_ids:
            return {}
        stmt = text(
            """
            SELECT m.id            AS id,
                   m.member_type   AS member_type,
                   m.name          AS name,
                   m.iban          AS iban,
                   i.email         AS individual_email,
                   i.social_rate   AS social_rate,
                   co.vat_number   AS company_vat,
                   mgr.email       AS manager_email,
                   ba.street       AS street,
                   ba.number       AS number,
                   ba.postcode     AS postcode,
                   ba.city         AS city,
                   ba.supplement   AS supplement
            FROM member m
            LEFT JOIN individual i  ON i.id = m.id
            LEFT JOIN company    co ON co.id = m.id
            LEFT JOIN manager    mgr ON mgr.id = COALESCE(co.id_manager, i.id_manager)
            LEFT JOIN address    ba ON ba.id = m.id_billing_address
            WHERE m.id_community = :cid AND m.id IN :ids
            """
        ).bindparams(bindparam("ids", expanding=True))
        result = await self._session.execute(stmt, {"cid": id_community, "ids": list(member_ids)})
        contacts: dict[int, ParticipantContact] = {}
        for row in result.mappings():
            contacts[row["id"]] = ParticipantContact(
                id=row["id"],
                member_type=row["member_type"],
                name=row["name"],
                iban=row["iban"],
                email=row["individual_email"] or row["manager_email"],
                vat_number=row["company_vat"],
                social_rate=bool(row["social_rate"]) if row["social_rate"] is not None else False,
                street=row["street"],
                # CRM `address.number` is an integer column; DTO expects `str | None`.
                number=str(row["number"]) if row["number"] is not None else None,
                postcode=row["postcode"],
                city=row["city"],
                supplement=row["supplement"],
            )
        return contacts

    async def member_ids_for_user(
        self, *, id_community: int, auth_user_id: str
    ) -> list[int]:
        """The member id(s) the authenticated user represents in this community."""
        result = await self._session.execute(
            text(
                """
                SELECT DISTINCT m.id AS id
                FROM member m
                JOIN user_member_link uml ON uml.id_member = m.id
                JOIN app_user au ON au.id = uml.id_user
                WHERE m.id_community = :cid AND au.auth_user_id = :auth_user_id
                ORDER BY m.id
                """
            ),
            {"cid": id_community, "auth_user_id": auth_user_id},
        )
        return [int(row["id"]) for row in result.mappings()]
