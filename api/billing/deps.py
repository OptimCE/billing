"""FastAPI dependencies that assemble the BillingService for a request."""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.billing.repository import BillingRepository
from api.billing.service import BillingService
from core.audit_log import AuditLogService
from core.config import settings
from core.database.database import get_crm_session, get_local_session
from ports.crm_core_sqlalchemy import SqlAlchemyCrmCoreRead
from ports.email import EmailPort
from ports.events import EventPublisher

# Which adapter backs each port is decided in `ports/providers.py`, not here, so
# the worker can make the same choice without importing fastapi. Re-exported
# because `dependency_overrides[deps.get_event_publisher]` keys on this object.
from ports.providers import get_email_port, get_event_publisher
from regime.registry import get_registry

__all__ = ["get_billing_service", "get_email_port", "get_event_publisher"]


def get_billing_service(
    local_session: AsyncSession = Depends(get_local_session),
    crm_session: AsyncSession = Depends(get_crm_session),
    publisher: EventPublisher = Depends(get_event_publisher),
    email: EmailPort = Depends(get_email_port),
) -> BillingService:
    return BillingService(
        local_session=local_session,
        crm_session=crm_session,
        repository=BillingRepository(local_session),
        crm_read=SqlAlchemyCrmCoreRead(crm_session),
        registry=get_registry(),
        publisher=publisher,
        email=email,
        audit=AuditLogService(crm_session),
        settings=settings,
    )
