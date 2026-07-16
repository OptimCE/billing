"""Tenant context for worker handlers.

Every message carries a ``tenant_id`` (== the internal ``id_community``). A
handler sets it into the same ContextVar the request path uses so
``with_community_scope`` filters correctly on both engines, then resets it.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from core.context_vars import current_internal_community_id


@contextlib.contextmanager
def with_tenant(id_community: int) -> Iterator[None]:
    token = current_internal_community_id.set(id_community)
    try:
        yield
    finally:
        current_internal_community_id.reset(token)
