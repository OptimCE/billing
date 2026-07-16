"""Object storage facade.

Wraps an S3-compatible client (MinIO in dev/staging/production) behind a tiny
function-shaped API so callers in ``api/generation/service.py`` and
``worker/dispatcher.py`` don't need to know about aiobotocore directly.

Exposed surface:
- ``upload(key, content, content_type=None)`` — put_object
- ``download(key) -> bytes`` — get_object
- ``delete(key)`` — best-effort delete_object
- ``ObjectNotFound`` — raised by ``download`` when the key does not exist
- ``TransientStorageError`` — raised by ``download`` for retryable errors
"""

from core.storage.client import (
    ObjectNotFound,
    TransientStorageError,
    delete,
    download,
    upload,
)

__all__ = [
    "ObjectNotFound",
    "TransientStorageError",
    "delete",
    "download",
    "upload",
]
