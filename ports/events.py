"""EventPublisher — a thin seam over NATS JetStream publish, injected into the
API so tests can substitute a fake (the ASGI test client never initialises NATS).
"""

from __future__ import annotations

from typing import Protocol

from core.queue.helper import Event, send_event
from core.queue.init import get_jetstream


class EventPublisher(Protocol):
    async def publish(self, subject: str, event: Event) -> None: ...


class NatsEventPublisher:
    """Publishes to the live JetStream context (set up by the app lifespan)."""

    async def publish(self, subject: str, event: Event) -> None:
        await send_event(get_jetstream(), subject, event)
