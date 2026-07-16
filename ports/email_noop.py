"""NoopEmailAdapter — logs instead of sending, until the email service exists."""

from __future__ import annotations

import logging

from ports.email import EmailMessage

logger = logging.getLogger(__name__)


class NoopEmailAdapter:
    async def send(self, message: EmailMessage) -> None:
        logger.info(
            "noop email → to=%s subject=%s attachment=%s",
            message.to,
            message.subject,
            message.attachment_ref,
        )
