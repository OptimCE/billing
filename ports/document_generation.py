"""DocumentGenerationPort — request a PDF from the document-generation service.

The service is async over NATS: we publish a FLAT request body (no Event
envelope) to ``docgen.request`` and the rendered result arrives later on the
``reply_to`` subject (``docgen.result.billing``), correlated by ``request_id``.
So this port is fire-and-forget; the result is handled by the docgen-results
consumer, not awaited here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class DocgenRequest:
    request_id: str
    tenant_id: str
    template_uri: str
    data: dict
    key_prefix: str
    reply_to: str
    locale: str
    presign_ttl: int
    metadata: dict = field(default_factory=dict)


class DocumentGenerationPort(Protocol):
    async def request_render(self, request: DocgenRequest) -> None: ...
