"""CrmCoreReadPort — the read-only contract over the CRM core database.

This service never writes to the CRM DB and depends on THIS interface, not on
the CRM table layout, so a core schema change is absorbed in the SQLAlchemy
adapter alone (ports/crm_core_sqlalchemy.py). All volumes are returned as raw
summed kWh (Decimal); the KWH_SCALE unit factor is applied by the caller when
freezing the snapshot.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class CommunityIdentity:
    """The invoicing legal entity + its headquarters address (payment block)."""

    id: int
    regulator: str
    legal_name: str | None
    vat_number: str | None
    iban: str | None
    account_holder_name: str | None
    street: str | None
    number: str | None
    postcode: str | None
    city: str | None
    supplement: str | None

    @property
    def has_billing_info(self) -> bool:
        """Pre-flight gate: a valid IBAN + a legal name are the minimum needed."""
        return bool((self.iban or "").strip()) and bool((self.legal_name or "").strip())


@dataclass(frozen=True)
class EanMemberAggregate:
    """Summed settlement volumes for one (EAN, owner) over the period (raw, unscaled).

    Readings are attributed to the member whose meter_data window contains the
    reading's Brussels-local date, so a meter that changed owner mid-period
    yields one aggregate per owner. ``id_member is None`` collects orphan
    volume — readings covered by no ownership window.
    """

    ean: str
    id_member: int | None
    client_type: int | None
    injection_status: int | None
    owned_from: date | None  # earliest matched window start; None for orphans
    owned_to: date | None  # latest matched window end; None if open-ended or orphan
    shared_sum: Decimal
    inj_shared_sum: Decimal
    row_count: int
    distinct_ts: int

    @property
    def has_duplicate_rows(self) -> bool:
        """True if the same (ean, timestamp) appears more than once → double import."""
        return self.row_count != self.distinct_ts


@dataclass(frozen=True)
class ParticipantContact:
    """Billing-contact details for a member (participant)."""

    id: int
    member_type: int
    name: str
    iban: str | None
    email: str | None
    vat_number: str | None
    social_rate: bool
    street: str | None
    number: str | None
    postcode: str | None
    city: str | None
    supplement: str | None


class CrmCoreReadPort(Protocol):
    async def consumption_exists(
        self,
        *,
        id_community: int,
        id_sharing_operation: int,
        period_start: date,
        period_end: date,
    ) -> bool: ...

    async def aggregate_by_ean_member(
        self,
        *,
        id_community: int,
        id_sharing_operation: int,
        period_start: date,
        period_end: date,
    ) -> list[EanMemberAggregate]: ...

    async def find_ownership_overlaps(
        self,
        *,
        id_community: int,
        id_sharing_operation: int,
        period_start: date,
        period_end: date,
    ) -> list[str]: ...

    async def get_community_identity(self, *, id_community: int) -> CommunityIdentity | None: ...

    async def participant_contacts(
        self, *, id_community: int, member_ids: Sequence[int]
    ) -> dict[int, ParticipantContact]: ...

    async def member_ids_for_user(self, *, id_community: int, auth_user_id: str) -> list[int]: ...
