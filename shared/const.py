from enum import IntEnum, StrEnum


class BillingRunStatus(IntEnum):
    """Lifecycle of a billing run (owned DB ``billing_run.status``)."""

    PENDING = 0
    COMPUTING = 1
    COMPUTED = 2
    FAILED = 3


class InvoiceStatus(IntEnum):
    """Invoice lifecycle (owned DB ``invoice.status``).

    See the state machine in the spec: DRAFT → ISSUED → SENT → PAID, with
    OVERDUE derived and CANCELLED reachable only from DRAFT. RENDER_FAILED marks
    an ISSUED invoice whose PDF render failed permanently (the invoice keeps its
    legal number; the render can be retried).

    CREDITED is terminal: an issued consumer INVOICE moves there when a credit
    note correcting it is ISSUED (the credit note is the legal act, not its
    DRAFT). The invoice's number, lines and amounts never change; only the
    status says it no longer stands, so it is neither payable nor swept OVERDUE.
    """

    DRAFT = 0
    ISSUED = 1
    SENT = 2
    PAID = 3
    OVERDUE = 4
    CANCELLED = 5
    RENDER_FAILED = 6
    CREDITED = 7


# Statuses an INVOICE can be credited from: every issued (numbered) state that
# still stands. A PAID invoice is included: crediting it is how a refund is owed.
CREDITABLE_STATUSES: frozenset[InvoiceStatus] = frozenset(
    {
        InvoiceStatus.ISSUED,
        InvoiceStatus.SENT,
        InvoiceStatus.OVERDUE,
        InvoiceStatus.RENDER_FAILED,
        InvoiceStatus.PAID,
    }
)


class InvoiceType(IntEnum):
    """Document type. Each type has its own gapless numbering series.

    Only an INVOICE is a receivable (the member pays the community). A
    CREDIT_NOTE cancels an invoice, and a PRODUCER_STATEMENT is a payout from
    the community to the producer, so neither carries a structured
    communication, goes OVERDUE, or uses the invoice notification wording.
    """

    INVOICE = 1
    CREDIT_NOTE = 2
    PRODUCER_STATEMENT = 3


class TariffKind(IntEnum):
    """The two independent pricing axes. Prices are community-set free fields."""

    CONSUMER_SELLING = 1
    PRODUCER_BUYBACK = 2


class TariffScope(IntEnum):
    """Tariff override granularity. Resolution is most-specific-wins EAN → SEGMENT → GLOBAL."""

    GLOBAL = 1  # operation-wide default
    SEGMENT = 2  # keyed to meter_data.client_type
    EAN = 3  # a single meter


class BillingDirection(IntEnum):
    """Which side of the sharing a snapshot/line represents."""

    CONSUMER = 1  # billed for `shared`
    PRODUCER = 2  # remunerated for `inj_shared`


class Measure(IntEnum):
    """The billable meter_consumption measure a line prices."""

    SHARED = 1
    INJ_SHARED = 2


class PaymentMethod(IntEnum):
    BANK_TRANSFER = 1
    DIRECT_DEBIT = 2
    CASH = 3
    OTHER = 4


class FeatureName(StrEnum):
    """CRM subscription feature gate key (see require_feature)."""

    BILLING = "billing"


# --- NATS JetStream streams (declared in core/queue/streams.json) ----------
# BILLING holds the work subjects (each consumed exactly once by one durable
# consumer → work_queue). BILLING_EVENTS holds fire-and-forget notifications
# (0..N observers → limits retention). BILLING_DLQ parks poison messages.
# Subjects across the three are disjoint, as JetStream requires.
BILLING_STREAM = "BILLING"
BILLING_EVENTS_STREAM = "BILLING_EVENTS"
BILLING_DLQ_STREAM = "BILLING_DLQ"

# --- Subjects the API publishes and the worker consumes --------------------
SUBJECT_RUN_REQUESTED = "optimce.billing.run.requested"
SUBJECT_RUN_COMPLETED = "optimce.billing.run.completed"
SUBJECT_INVOICE_ISSUE_REQUESTED = "optimce.billing.invoice.issue.requested"
SUBJECT_INVOICE_RENDERED = "optimce.billing.invoice.rendered"
SUBJECT_OVERDUE_SWEEP = "optimce.billing.overdue.sweep"

# --- Dead-letter subjects (BILLING_DLQ stream) -----------------------------
SUBJECT_DLQ_RUN = "optimce.billing.dlq.run"
SUBJECT_DLQ_ISSUE = "optimce.billing.dlq.issue"
SUBJECT_DLQ_DOCGEN_RESULT = "optimce.billing.dlq.docgen_result"
