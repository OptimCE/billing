"""The scheduled overdue sweep.

`POST /billing-runs/overdue-sweep` has existed since invoicing landed and
nothing ever invoked it, so no invoice was ever marked overdue on its own and
`invoice.overdue` — which is TRANSACTIONAL and goes to the member's inbox —
could not fire.

This module is that scheduler. It calls the same `BillingService` the route
calls, so there is exactly one implementation of the sweep.
"""

from __future__ import annotations

import datetime
import logging
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.billing.repository import BillingRepository
from api.billing.service import BillingService
from core.audit_log import AuditLogService
from core.config import settings
from core.database.database import AsyncSessionCRMFactory, AsyncSessionLocalFactory
from ports.crm_core_sqlalchemy import SqlAlchemyCrmCoreRead

# `ports.providers`, NOT `api.billing.deps`: the providers are the same objects,
# but `deps` imports fastapi, which `requirements/worker.txt` does not install —
# so importing it here crash-loops the worker image on startup.
from ports.providers import get_email_port, get_event_publisher
from regime.registry import get_registry
from shared.const import InvoiceStatus
from shared.models.local_models import InvoiceModel
from worker.context import with_tenant

logger = logging.getLogger(__name__)

_SETTLEMENT_TZ = ZoneInfo("Europe/Brussels")

# One advisory lock for the whole sweep, so two worker replicas cannot both run
# it. `dedupe_key` would collapse the duplicate EMAIL, but nothing collapses the
# duplicate in-app notification. Namespaced by service; must stay stable.
_SWEEP_ADVISORY_LOCK_KEY = 0x0B111_0001


async def _communities_with_overdue_invoices_unscoped(
    local: AsyncSession, *, today: datetime.date
) -> list[int]:
    """Which communities have an invoice the sweep would flip.

    **Deliberately unscoped**, hence the name. Every other owned-DB read goes
    through `with_community_scope`, which filters on the
    `current_internal_community_id` ContextVar — but a scheduler has no request
    and therefore no tenant yet, and a scoped read here would match nothing with
    no error, which is indistinguishable from "no work to do".
    """
    stmt = (
        select(InvoiceModel.id_community)
        .where(
            InvoiceModel.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.SENT]),
            InvoiceModel.due_date < today,
        )
        .distinct()
    )
    return list((await local.execute(stmt)).scalars().all())


async def sweep_overdue_for_every_community(
    *,
    local_session: AsyncSession | None = None,
    crm_session: AsyncSession | None = None,
    today: datetime.date | None = None,
) -> tuple[int, int]:
    """Run the overdue sweep for every community with something to flip.

    Sessions are injectable, mirroring `process_billing_run`, so a test can drive
    this on a rolled-back session without a container.
    Returns (invoices marked, communities swept).
    """
    own_local = local_session is None
    own_crm = crm_session is None
    local = local_session or AsyncSessionLocalFactory()
    crm = crm_session or AsyncSessionCRMFactory()
    as_of = today or datetime.datetime.now(_SETTLEMENT_TZ).date()
    try:
        community_ids = await _communities_with_overdue_invoices_unscoped(local, today=as_of)
        if not community_ids:
            return 0, 0

        # The real service, not a reimplementation: `sweep_overdue` uses none of
        # `registry`/`publisher`/`email`, but constructing the genuine article is
        # what keeps the route and the scheduler on one code path.
        service = BillingService(
            local_session=local,
            crm_session=crm,
            repository=BillingRepository(local),
            crm_read=SqlAlchemyCrmCoreRead(crm),
            registry=get_registry(),
            publisher=get_event_publisher(),
            email=get_email_port(),
            audit=AuditLogService(crm),
            settings=settings,
        )
        marked_total = 0
        swept = 0
        for id_community in community_ids:
            # Without this the sweep's UPDATE filters on a None community and
            # silently matches nothing.
            with with_tenant(id_community):
                result = await service.sweep_overdue()
            marked_total += result.marked
            swept += 1
        logger.info(
            "overdue sweep: %s communit(ies), %s invoice(s) marked",
            swept,
            marked_total,
            extra={"operation": "worker:overdue_sweep"},
        )
        return marked_total, swept
    finally:
        if own_local:
            await local.close()
        if own_crm:
            await crm.close()


async def try_sweep_overdue() -> bool:
    """Take the advisory lock and sweep. Returns False if another replica has it.

    `pg_try_advisory_lock` is session-scoped, so the lock lives exactly as long
    as this connection and is released even if the process dies — the property a
    cron-style lock needs and a lock table does not have.
    """
    from sqlalchemy import func

    async with AsyncSessionLocalFactory() as local:
        acquired = await local.scalar(select(func.pg_try_advisory_lock(_SWEEP_ADVISORY_LOCK_KEY)))
        if not acquired:
            logger.info("overdue sweep skipped: another replica holds the lock")
            return False
        try:
            async with AsyncSessionCRMFactory() as crm:
                await sweep_overdue_for_every_community(local_session=local, crm_session=crm)
            return True
        finally:
            await local.execute(select(func.pg_advisory_unlock(_SWEEP_ADVISORY_LOCK_KEY)))
