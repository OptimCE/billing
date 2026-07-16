import logging
from functools import wraps

from core.errors.errors import Error, ErrorException

logger = logging.getLogger(__name__)


def with_default_error(default_error: Error):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except ErrorException:
                # An already-classified domain error (e.g. a 4xx): the global
                # handler logs it (with code + key) and renders the response.
                raise
            except Exception as e:
                logger.error("Unexpected error, promoting to %s", default_error, exc_info=e)
                raise ErrorException(default_error) from e  # promote to domain error

        return wrapper

    return decorator
