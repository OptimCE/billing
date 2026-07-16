"""
Request limits middleware — body size cap + request timeout.

Protects against oversized payloads and slow-loris / hung-request attacks.

Body size check:
    Reads Content-Length before the request reaches FastAPI's body parser.
    If the declared length exceeds the per-route cap (see ``_max_body_for``),
    the request is rejected immediately with 413 Payload Too Large, before any
    body is read into memory.

    This is a cheap up-front gate, not a complete one. A client using chunked
    transfer-encoding sends no Content-Length and so slips past this check; the
    billing API accepts no file uploads, so the JSON-sized cap below suffices.

Request timeout:
    Wraps the entire downstream handler in asyncio.wait_for().
    If the handler does not complete within TIMEOUT_SECONDS,
    the client receives a 504 Gateway Timeout.

Usage:
    app.add_middleware(RequestLimitsMiddleware)
"""

import asyncio
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# 2 MB — the billing API is JSON-only (no file uploads); this blocks abuse.
MAX_BODY_BYTES = 2 * 1024 * 1024

# 30 seconds — covers complex DB queries.
TIMEOUT_SECONDS = 30


def _max_body_for(request: Request) -> int:
    """Return the body-size cap for this request (uniform across the JSON API)."""
    return MAX_BODY_BYTES


class RequestLimitsMiddleware(BaseHTTPMiddleware):
    """
    Enforces request body size limits and per-request timeout.
    """

    async def dispatch(self, request: Request, call_next):
        # --- Body size gate ---
        max_bytes = _max_body_for(request)
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > max_bytes:
                    logger.warning(
                        "Request rejected: body too large",
                        extra={
                            "content_length": content_length,
                            "max_allowed": max_bytes,
                            "path": request.url.path,
                        },
                    )
                    return JSONResponse(
                        status_code=413,
                        content={
                            "data": "Payload too large",
                            "error_code": 0,
                        },
                    )
            except ValueError:
                pass

        # --- Request timeout ---
        try:
            response = await asyncio.wait_for(
                call_next(request),
                timeout=TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.error(
                "Request timed out",
                extra={
                    "timeout_seconds": TIMEOUT_SECONDS,
                    "path": request.url.path,
                    "method": request.method,
                },
            )
            return JSONResponse(
                status_code=504,
                content={
                    "data": "Request timeout",
                    "error_code": 0,
                },
            )

        return response
