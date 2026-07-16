"""Tariff resolution precedence (EAN → SEGMENT → GLOBAL), against real Postgres."""

from __future__ import annotations

import datetime
from decimal import Decimal

from api.billing.repository import BillingRepository
from core.context_vars import current_internal_community_id
from shared.const import TariffKind, TariffScope
from shared.models.local_models import TariffModel
from tests.factories import crm_billing_factory as f

_JAN = datetime.date(2026, 1, 1)
_JUN = datetime.date(2026, 6, 1)


def _tariff(op: int, *, scope: int, price: str, segment=None, ean=None) -> TariffModel:
    return TariffModel(
        id_sharing_operation=op,
        kind=TariffKind.CONSUMER_SELLING,
        scope=scope,
        scope_segment=segment,
        scope_ean=ean,
        price_per_kwh=Decimal(price),
        currency="EUR",
        valid_from=_JAN,
    )


async def test_resolve_price_precedence(db_session):
    cid = await f.create_community(db_session)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    token = current_internal_community_id.set(cid)
    try:
        repo = BillingRepository(db_session)
        await repo.create_tariff(_tariff(op, scope=TariffScope.GLOBAL, price="0.10"))
        await repo.create_tariff(_tariff(op, scope=TariffScope.SEGMENT, segment=2, price="0.20"))
        await repo.create_tariff(_tariff(op, scope=TariffScope.EAN, ean="EAN-1", price="0.30"))

        # EAN-1 + segment 2 → EAN wins.
        got = await repo.resolve_price(
            id_sharing_operation=op, kind=TariffKind.CONSUMER_SELLING,
            ean="EAN-1", client_type=2, on_date=_JUN,
        )
        assert got is not None and got.price_per_kwh == Decimal("0.30")

        # EAN-2 (no EAN tariff) + segment 2 → SEGMENT wins.
        got = await repo.resolve_price(
            id_sharing_operation=op, kind=TariffKind.CONSUMER_SELLING,
            ean="EAN-2", client_type=2, on_date=_JUN,
        )
        assert got is not None and got.price_per_kwh == Decimal("0.20")

        # EAN-2 + segment 1 (no segment tariff) → GLOBAL.
        got = await repo.resolve_price(
            id_sharing_operation=op, kind=TariffKind.CONSUMER_SELLING,
            ean="EAN-2", client_type=1, on_date=_JUN,
        )
        assert got is not None and got.price_per_kwh == Decimal("0.10")
    finally:
        current_internal_community_id.reset(token)


async def test_resolve_price_none_when_no_tariff_or_outside_window(db_session):
    cid = await f.create_community(db_session)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    token = current_internal_community_id.set(cid)
    try:
        repo = BillingRepository(db_session)
        await repo.create_tariff(_tariff(op, scope=TariffScope.GLOBAL, price="0.10"))

        # Wrong kind (no producer tariff) → None.
        assert (
            await repo.resolve_price(
                id_sharing_operation=op, kind=TariffKind.PRODUCER_BUYBACK,
                ean="EAN-2", client_type=1, on_date=_JUN,
            )
            is None
        )
        # Before valid_from → None.
        assert (
            await repo.resolve_price(
                id_sharing_operation=op, kind=TariffKind.CONSUMER_SELLING,
                ean="EAN-2", client_type=1, on_date=datetime.date(2025, 12, 31),
            )
            is None
        )
    finally:
        current_internal_community_id.reset(token)
