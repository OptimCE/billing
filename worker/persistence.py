"""Worker-side billing-run processing (price snapshots → DRAFT invoices).

Extracted from the NATS dispatcher so it can be driven directly in tests. Opens
its own short-lived sessions in production; tests inject a shared session. The
whole run is idempotent: a redelivery finds the run already COMPUTED (or the
conditional claim fails) and no-ops.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from api.billing.repository import BillingRepository
from core import metrics as app_metrics
from core.audit_log import AuditActions, AuditLogInput, AuditLogService
from core.database.database import AsyncSessionCRMFactory, AsyncSessionLocalFactory
from core.notifications import (
    MANAGER_ROLES,
    Channel,
    CommunityTarget,
    NotificationCategory,
    NotificationService,
    NotificationTypes,
)
from core.realtime import CommunityAudience, Tier, emit
from regime.registry import RegimeConfigError, get_registry
from shared.const import BillingDirection, BillingRunStatus, TariffKind
from shared.models.local_models import BillingRunModel
from worker import pricing
from worker.context import with_tenant

logger = logging.getLogger(__name__)


class DeterministicRunError(Exception):
    """A non-retryable run failure: the caller marks the run FAILED and acks."""


async def process_billing_run(
    run_id: int,
    *,
    local_session: AsyncSession | None = None,
    crm_session: AsyncSession | None = None,
) -> int:
    """Price a run's frozen snapshots into DRAFT invoices. Returns the count."""
    own_local = local_session is None
    own_crm = crm_session is None
    local = local_session or AsyncSessionLocalFactory()
    crm = crm_session or AsyncSessionCRMFactory()
    try:
        repo = BillingRepository(local)
        run = await repo.get_run_by_id(run_id)
        if run is None:
            logger.warning("process_billing_run: run %s not found", run_id)
            return 0

        with with_tenant(run.id_community):
            if run.status == BillingRunStatus.COMPUTED:
                return await repo.count_invoices_for_run(run_id)
            if not await repo.claim_run_computing(run_id):
                return await repo.count_invoices_for_run(run_id)

            try:
                regime = get_registry().get_for(run.regulator)
            except RegimeConfigError as exc:
                raise DeterministicRunError(f"no regime for regulator {run.regulator}") from exc

            snapshots = await repo.list_snapshots(run_id)
            prices: dict[int, tuple] = {}
            for snapshot in snapshots:
                quantity = (
                    snapshot.shared_kwh
                    if snapshot.direction == BillingDirection.CONSUMER
                    else snapshot.inj_shared_kwh
                )
                if snapshot.id_member is None or quantity <= 0:
                    continue
                kind = (
                    TariffKind.CONSUMER_SELLING
                    if snapshot.direction == BillingDirection.CONSUMER
                    else TariffKind.PRODUCER_BUYBACK
                )
                tariff = await repo.resolve_price(
                    id_sharing_operation=run.id_sharing_operation,
                    kind=kind,
                    ean=snapshot.ean,
                    client_type=snapshot.client_type,
                    on_date=run.period_start,
                )
                if tariff is None:
                    raise DeterministicRunError(
                        f"no tariff resolves for EAN {snapshot.ean} (kind {int(kind)})"
                    )
                prices[snapshot.id] = (tariff.price_per_kwh, tariff.currency)

            invoices = pricing.build_invoices(
                id_community=run.id_community,
                id_billing_run=run_id,
                snapshots=snapshots,
                prices=prices,
                regime=regime,
            )
            await repo.add_invoices(invoices)
            await repo.set_run_status(run_id, BillingRunStatus.COMPUTED)
            if own_local:
                await local.commit()
            count = len(invoices)
            app_metrics.billing_runs_completed.add(1, {"status": "computed"})

        await AuditLogService(crm).log(
            AuditLogInput(
                action=AuditActions.RUN_COMPUTED,
                entity_type="billing_run",
                entity_id=str(run_id),
                payload={"invoice_count": count},
            ),
            id_community=run.id_community,
        )
        # Same CRM transaction as the audit row. `with_tenant` has already exited
        # here, so the community is passed explicitly — exactly as the audit call
        # above does, and core/notifications never reads a ContextVar. Redelivery
        # safety is free: both early-return paths return from inside the
        # `with_tenant` block, skipping the audit and this notification alike.
        # Hoisted to a local: the realtime hint is staged on the INSTANCE by
        # publish() and released by flush_realtime() after the commit, so
        # constructing it inline (as this used to) would leave the staged hint
        # unreachable and silently drop every event from this path.
        notifier = NotificationService(crm)
        await notifier.publish(
            type=NotificationTypes.BILLING_RUN_COMPLETED,
            target=CommunityTarget(community_id=run.id_community, roles=MANAGER_ROLES),
            category=NotificationCategory.INFORMATIONAL,
            channels=(Channel.INAPP,),
            data={"run_id": run_id, "invoice_count": count},
        )
        if own_crm:
            await crm.commit()
            # BOTH of these live inside `if own_crm:` on purpose. When a session
            # is injected the CALLER owns the commit, so emitting here would
            # publish pre-commit — and the fire-and-forget transport gives no
            # second chance. An injected-session caller owns its own flush.
            await notifier.flush_realtime()
            # Retires the 4s poll on the billing run card. Community MANAGER
            # tier, matching the notification's own audience above.
            await emit(
                topic="billing_run.finished",
                audience=CommunityAudience(community_id=run.id_community, tier=Tier.MANAGER),
                resource=("billing_run", run_id),
                scope_community_id=run.id_community,
                hint={"status": "success"},
            )
        return count
    finally:
        if own_local:
            await local.close()
        if own_crm:
            await crm.close()


async def mark_run_failed(run_id: int, error_message: str) -> None:
    """Mark a run FAILED in a fresh session (used by the dispatcher on ack-fail)."""
    async with AsyncSessionLocalFactory() as session:
        run = await session.get(BillingRunModel, run_id)
        if run is None:
            return
        with with_tenant(run.id_community):
            await BillingRepository(session).set_run_status(
                run_id, BillingRunStatus.FAILED, error_message=error_message[:2000]
            )
            await session.commit()
