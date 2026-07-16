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
from ports.email_noop import NoopEmailAdapter
from ports.events import EventPublisher, NatsEventPublisher
from regime.registry import get_registry


def get_event_publisher() -> EventPublisher:
    return NatsEventPublisher()


def get_email_port() -> EmailPort:
    return NoopEmailAdapter()


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
