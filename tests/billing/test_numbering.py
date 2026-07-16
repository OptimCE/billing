"""Gapless numbering: sequential contiguity + concurrency (two real connections)."""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from api.billing.repository import BillingRepository
from core.context_vars import current_internal_community_id
from tests.factories import crm_billing_factory as f

_TEST_DB_URL = "postgresql+asyncpg://postgres:postgres@localhost:5433/test_db_be"


async def test_claim_next_number_is_gapless_per_series_and_year(db_session):
    cid = await f.create_community(db_session)
    token = current_internal_community_id.set(cid)
    try:
        repo = BillingRepository(db_session)
        got = [
            await repo.claim_next_number(legal_entity_key="community:X:F", year=2026)
            for _ in range(5)
        ]
        assert got == [1, 2, 3, 4, 5]
        # A different series numbers independently.
        assert await repo.claim_next_number(legal_entity_key="community:X:NC", year=2026) == 1
        # A different year restarts.
        assert await repo.claim_next_number(legal_entity_key="community:X:F", year=2027) == 1
    finally:
        current_internal_community_id.reset(token)


async def test_claim_next_number_concurrent_has_no_gaps_or_duplicates():
    """Ten concurrent issues on two-plus real connections → contiguous, unique."""
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    lek = "community:concurrency:F"
    year = 2099
    auth = "concurrency-test-auth"

    try:
        # Fresh committed community (this test commits for real, outside the rollback).
        async with session_factory() as setup:
            await setup.execute(
                text("DELETE FROM invoice_sequence WHERE legal_entity_key = :lek"), {"lek": lek}
            )
            await setup.execute(
                text("DELETE FROM community WHERE auth_community_id = :auth"), {"auth": auth}
            )
            cid = int(
                (
                    await setup.execute(
                        text(
                            "INSERT INTO community (name, auth_community_id) "
                            "VALUES (:auth, :auth) RETURNING id"
                        ),
                        {"auth": auth},
                    )
                ).scalar_one()
            )
            await setup.commit()

        async def claim() -> int:
            current_internal_community_id.set(cid)
            async with session_factory() as session:
                value = await BillingRepository(session).claim_next_number(
                    legal_entity_key=lek, year=year
                )
                await session.commit()
                return value

        results = await asyncio.gather(*[claim() for _ in range(10)])
        assert sorted(results) == list(range(1, 11))  # gapless + unique under contention

        async with session_factory() as cleanup:
            await cleanup.execute(
                text("DELETE FROM invoice_sequence WHERE legal_entity_key = :lek"), {"lek": lek}
            )
            await cleanup.execute(text("DELETE FROM community WHERE id = :cid"), {"cid": cid})
            await cleanup.commit()
    finally:
        await engine.dispose()
