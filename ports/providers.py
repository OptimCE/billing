"""Which adapter backs each port — chosen here, framework-free.

The choice used to live in ``api/billing/deps.py``, which meant importing it
dragged in ``fastapi``. That is fine for the API and fatal for the worker:
``Dockerfile.worker`` installs ``requirements/worker.txt`` (no fastapi, no
uvicorn) precisely to keep the HTTP stack out of that image, so the scheduler's
``from api.billing.deps import ...`` crash-looped the container on every start.

So the selection lives here, next to the adapters, and both callers import it:
``api/billing/deps.py`` wraps these in ``Depends``, ``worker/sweeps.py`` calls
them directly. One definition, so the request path and the daily tick cannot
drift onto different adapters.
"""

from __future__ import annotations

from ports.email import EmailPort
from ports.email_noop import NoopEmailAdapter
from ports.events import EventPublisher, NatsEventPublisher


def get_event_publisher() -> EventPublisher:
    return NatsEventPublisher()


def get_email_port() -> EmailPort:
    return NoopEmailAdapter()
