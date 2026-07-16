"""Body-size cap for the request-limits middleware.

The billing API is JSON-only, so every route shares the conservative
``MAX_BODY_BYTES`` cap (there is no larger upload cap).
"""

from __future__ import annotations

from types import SimpleNamespace

from core.middleware import request_limits


def _request(method: str, path: str) -> SimpleNamespace:
    return SimpleNamespace(method=method, url=SimpleNamespace(path=path))


def test_cap_is_uniform_across_routes():
    for method, path in (
        ("POST", "/"),
        ("GET", "/invoices"),
        ("POST", "/sharing-operations/1/billing-runs"),
        ("DELETE", "/tariffs/1"),
    ):
        assert (
            request_limits._max_body_for(_request(method, path)) == request_limits.MAX_BODY_BYTES
        )
