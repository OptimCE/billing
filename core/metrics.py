"""Application metrics for the billing service.

All instruments are created at module import. The OTel API ships a
``_ProxyMeterProvider`` until ``metrics.set_meter_provider(...)`` runs
(inside ``core.tracing.setup_tracer_provider``), and instruments created
against the proxy rebind to the real provider when it is installed —
so import order between this module and tracing setup does not matter.

In LOCAL ``setup_tracer_provider`` returns early and the proxy stays a
no-op, meaning every ``.add(...)``/``.record(...)`` call below becomes
a cheap function dispatch with no side effects.

Naming follows OTel semantic conventions: dotted lowercase, ``.total``
suffix on monotonically increasing counters.
"""

from __future__ import annotations

from collections.abc import Iterable

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

_meter = metrics.get_meter("billing")

billing_runs_completed = _meter.create_counter(
    name="billing.runs.completed.total",
    description="Billing runs finalized by the worker, labelled by status",
    unit="1",
)

invoices_issued = _meter.create_counter(
    name="billing.invoices.issued.total",
    description="Invoices issued (gapless-numbered) by the API",
    unit="1",
)

invoices_rendered = _meter.create_counter(
    name="billing.invoices.rendered.total",
    description="Invoice PDFs finalized from document-generation, labelled by outcome",
    unit="1",
)

worker_messages = _meter.create_counter(
    name="billing.worker.messages.total",
    description="NATS message handler outcomes, labelled by outcome",
    unit="1",
)

health_checks = _meter.create_counter(
    name="health.checks.total",
    description="Readiness probe component outcomes",
    unit="1",
)

# Latest queue depth per subject. Reserved for a future queue-depth poller; the
# observable gauge below reads from this dict on every metric collection cycle.
queue_depth_snapshot: dict[str, int] = {}


def _queue_depth_callback(options: CallbackOptions) -> Iterable[Observation]:
    """Emit one observation per subject with its current pending count."""
    return [
        Observation(value, {"subject": subject}) for subject, value in queue_depth_snapshot.items()
    ]


_meter.create_observable_gauge(
    name="billing.queue.depth",
    callbacks=[_queue_depth_callback],
    description="JetStream consumer num_pending per billing subject",
    unit="1",
)
