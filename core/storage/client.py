"""aiobotocore-backed S3 client used for source-file storage.

The service uploads user-provided source data files to ``STORAGE_BUCKET``
(MinIO in dev). The worker reads them back during generation and deletes
them once the row reaches a terminal state. The shape is deliberately
function-based — there's no client singleton to wire into the FastAPI
lifespan or the worker startup. aiobotocore clients are async-context
managers; opening one per operation is cheap because the underlying
aiohttp connector pool is shared across the asyncio loop.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from aiobotocore.session import get_session
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from botocore.exceptions import (
    ConnectionError as BotoConnectionError,
)

from core.config import settings

logger = logging.getLogger(__name__)


class ObjectNotFound(Exception):  # noqa: N818  # public exception name; raised in dispatcher, caught in worker
    """Raised by ``download`` when the requested key is absent from the bucket.

    Treat as a deterministic failure: the file is gone, no amount of retry
    will bring it back. Callers should mark the generation FAILED and ack.
    """


class TransientStorageError(Exception):
    """Raised by ``download`` for retryable errors (5xx, timeouts, transport).

    Callers should let JetStream redeliver the message rather than write a
    terminal status.
    """


# The set of botocore error codes we treat as "the object isn't there".
# NoSuchKey is the canonical S3 code; AWS sometimes returns 404 without one.
_NOT_FOUND_CODES = {"NoSuchKey", "NoSuchBucket", "404"}


@asynccontextmanager
async def _client() -> AsyncIterator:
    """Yield a fresh aiobotocore S3 client bound to the configured endpoint.

    ``aiobotocore`` requires its clients to be used as async context managers
    so the underlying aiohttp connector is closed deterministically.
    """
    session = get_session()
    async with session.create_client(
        "s3",
        endpoint_url=settings.STORAGE_ENDPOINT or None,
        region_name=settings.STORAGE_REGION,
        aws_access_key_id=settings.STORAGE_ACCESS_KEY,
        aws_secret_access_key=settings.STORAGE_SECRET_KEY,
    ) as client:
        yield client


async def upload(key: str, content: bytes, content_type: str | None = None) -> None:
    """Upload ``content`` as the object at ``key`` in ``STORAGE_BUCKET``.

    Raises whatever ``put_object`` raises — the service layer is responsible
    for translating those into HTTP errors and rolling back. No retries: the
    caller decides whether to surface the failure or swallow it.
    """
    extra: dict[str, str] = {}
    if content_type:
        extra["ContentType"] = content_type
    async with _client() as s3:
        await s3.put_object(
            Bucket=settings.STORAGE_BUCKET,
            Key=key,
            Body=content,
            **extra,
        )


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """Split an ``s3://bucket/key`` URI into ``(bucket, key)``.

    Raises ``ValueError`` if the URI is not a well-formed ``s3://`` reference —
    the caller (serving a stored artifact) turns that into a domain error.
    """
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3 uri: {uri!r}")
    bucket, _, key = uri[len("s3://") :].partition("/")
    if not bucket or not key:
        raise ValueError(f"s3 uri missing bucket or key: {uri!r}")
    return bucket, key


async def _get(bucket: str, key: str) -> bytes:
    """Fetch the object at ``bucket/key`` and return its bytes.

    Classifies errors into the two buckets the worker dispatcher cares about:
    ``ObjectNotFound`` for missing keys (deterministic) and
    ``TransientStorageError`` for everything that smells like a network /
    server hiccup (retryable). Unexpected errors propagate so the dispatcher's
    catch-all path can mark the row as ``unhandled_worker_error``.
    """
    try:
        async with _client() as s3:
            response = await s3.get_object(Bucket=bucket, Key=key)
            async with response["Body"] as stream:
                return cast(bytes, await stream.read())
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if error_code in _NOT_FOUND_CODES or status == 404:
            raise ObjectNotFound(key) from exc
        if status is not None and 500 <= status < 600:
            raise TransientStorageError(f"storage {status} for {key}") from exc
        raise
    except (TimeoutError, EndpointConnectionError, BotoConnectionError, ReadTimeoutError) as exc:
        raise TransientStorageError(f"storage transport: {exc}") from exc


async def _remove(bucket: str, key: str) -> None:
    """Delete the object at ``bucket/key``. Idempotent and best-effort.

    Never raises: a leaked file is a soft failure compared to a stuck row.
    Missing keys are logged at DEBUG; other errors at WARN.
    """
    try:
        async with _client() as s3:
            await s3.delete_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in _NOT_FOUND_CODES:
            logger.debug("delete: object already gone key=%s", key)
            return
        logger.warning("delete failed for key=%s code=%s; leaking object", key, error_code)
    except Exception:
        logger.warning("delete failed for key=%s; leaking object", key, exc_info=True)


async def download(key: str) -> bytes:
    """Fetch the object at ``key`` from the source-file bucket (``STORAGE_BUCKET``)."""
    return await _get(settings.STORAGE_BUCKET, key)


async def delete(key: str) -> None:
    """Delete the object at ``key`` from the source-file bucket (``STORAGE_BUCKET``)."""
    await _remove(settings.STORAGE_BUCKET, key)


async def download_output(key: str) -> bytes:
    """Fetch a rendered document from the docgen output bucket (``OUTPUT_BUCKET``).

    Same error classification as :func:`download`; reads the document-generation
    output bucket rather than the source-file bucket, so billing can serve the
    PDFs the docgen worker writes there.
    """
    return await _get(settings.OUTPUT_BUCKET, key)


async def delete_output(key: str) -> None:
    """Best-effort delete of a rendered document from the output bucket."""
    await _remove(settings.OUTPUT_BUCKET, key)
