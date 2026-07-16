"""EmailPort — dispatch an invoice to a participant. v1 uses a Noop adapter; a
real adapter lands when the email service exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    body: str
    attachment_ref: str | None = None


class EmailPort(Protocol):
    async def send(self, message: EmailMessage) -> None: ...
