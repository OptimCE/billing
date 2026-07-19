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

from ports.crm_core import CommunityIdentity, EanMemberAggregate, ParticipantContact

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

    async def aggregate_by_ean_member(
        self,
        *,
        id_community: int,
        id_sharing_operation: int,
        period_start: date,
        period_end: date,
    ) -> list[EanMemberAggregate]:
        start, end_exclusive = _period_bounds(period_start, period_end)
        # Each reading is attributed to the meter_data window containing its
        # Brussels-local date (windows are inclusive [start_date, end_date],
        # consecutive owners never overlap — enforced by find_ownership_overlaps
        # before any run). LEFT JOIN keeps unowned readings as the NULL-member
        # group. client_type/injection_status come from the latest matched
        # window; owned_to is NULL while any matched window is still open.
        result = await self._session.execute(
            text(
                """
                SELECT mc.ean          AS ean,
                       md.id_member    AS id_member,
                       (ARRAY_AGG(md.client_type      ORDER BY md.start_date DESC))[1]
                                       AS client_type,
                       (ARRAY_AGG(md.injection_status ORDER BY md.start_date DESC))[1]
                                       AS injection_status,
                       MIN(md.start_date) AS owned_from,
                       CASE WHEN COUNT(*) FILTER (
                                WHERE md.id IS NOT NULL AND md.end_date IS NULL
                            ) > 0
                            THEN NULL ELSE MAX(md.end_date) END AS owned_to,
                       COALESCE(SUM(mc.shared), 0)     AS shared_sum,
                       COALESCE(SUM(mc.inj_shared), 0) AS inj_shared_sum,
                       COUNT(*)                        AS row_count,
                       COUNT(DISTINCT mc.timestamp)    AS distinct_ts
                FROM meter_consumption mc
                LEFT JOIN meter_data md
                       ON md.ean = mc.ean
                      AND md.id_sharing_operation = mc.id_sharing_operation
                      AND md.status = :active
                      AND (mc.timestamp AT TIME ZONE 'Europe/Brussels')::date
                          BETWEEN md.start_date AND COALESCE(md.end_date, 'infinity'::date)
                WHERE mc.id_community = :cid
                  AND mc.id_sharing_operation = :op
                  AND mc.timestamp >= :start AND mc.timestamp < :end_excl
                GROUP BY mc.ean, md.id_member
                ORDER BY mc.ean, md.id_member NULLS LAST
                """
            ),
            {
                "cid": id_community,
                "op": id_sharing_operation,
                "active": _ACTIVE_METER_DATA_STATUS,
                "start": start,
                "end_excl": end_exclusive,
            },
        )
        return [
            EanMemberAggregate(
                ean=row["ean"],
                id_member=row["id_member"],
                client_type=row["client_type"],
                injection_status=row["injection_status"],
                owned_from=row["owned_from"],
                owned_to=row["owned_to"],
                shared_sum=Decimal(str(row["shared_sum"])),
                inj_shared_sum=Decimal(str(row["inj_shared_sum"])),
                row_count=int(row["row_count"]),
                distinct_ts=int(row["distinct_ts"]),
            )
            for row in result.mappings()
        ]

    async def find_ownership_overlaps(
        self,
        *,
        id_community: int,
        id_sharing_operation: int,
        period_start: date,
        period_end: date,
    ) -> list[str]:
        """EANs with two ACTIVE meter_data windows that overlap inside the period.

        Such a state would silently attribute the same reading to two owners in
        aggregate_by_ean_member, so a run must be refused until the CRM history
        is repaired. Adjacent windows (end_date = next start_date - 1) do not
        overlap.
        """
        result = await self._session.execute(
            text(
                """
                SELECT DISTINCT a.ean AS ean
                FROM meter_data a
                JOIN meter_data b
                  ON b.ean = a.ean
                 AND b.id_sharing_operation = a.id_sharing_operation
                 AND b.id > a.id
                 AND b.status = :active
                WHERE a.id_community = :cid
                  AND a.id_sharing_operation = :op
                  AND a.status = :active
                  AND a.start_date <= COALESCE(b.end_date, 'infinity'::date)
                  AND b.start_date <= COALESCE(a.end_date, 'infinity'::date)
                  AND GREATEST(a.start_date, b.start_date) <= :period_end
                  AND LEAST(
                        COALESCE(a.end_date, 'infinity'::date),
                        COALESCE(b.end_date, 'infinity'::date)
                      ) >= :period_start
                ORDER BY a.ean
                """
            ),
            {
                "cid": id_community,
                "op": id_sharing_operation,
                "active": _ACTIVE_METER_DATA_STATUS,
                "period_start": period_start,
                "period_end": period_end,
            },
        )
        return [row["ean"] for row in result.mappings()]

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

    async def member_ids_for_user(self, *, id_community: int, auth_user_id: str) -> list[int]:
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
