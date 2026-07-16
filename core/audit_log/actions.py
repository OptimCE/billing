"""Audit log action codes.

Action codes follow the ``domain.entity.verb`` convention used by
``crm-backend`` (e.g. ``crm.allocation_key.created``). They are stored as
``VARCHAR(128)`` and the ``AuditAction`` type stays open-ended so call sites
can introduce new codes without round-tripping this module.
"""

from typing import Final

AuditAction = str


class AuditActions:
    """Known action codes emitted by the billing service."""

    # Billing runs
    RUN_CREATED: Final[AuditAction] = "billing.run.created"
    RUN_QUEUE_FAILED: Final[AuditAction] = "billing.run.queue_failed"
    RUN_COMPUTED: Final[AuditAction] = "billing.run.computed"
    RUN_FAILED: Final[AuditAction] = "billing.run.failed"

    # Invoices
    INVOICE_ISSUED: Final[AuditAction] = "billing.invoice.issued"
    INVOICE_RENDER_REQUESTED: Final[AuditAction] = "billing.invoice.render_requested"
    INVOICE_RENDERED: Final[AuditAction] = "billing.invoice.rendered"
    INVOICE_RENDER_FAILED: Final[AuditAction] = "billing.invoice.render_failed"
    INVOICE_PDF_DELETED: Final[AuditAction] = "billing.invoice.pdf_deleted"
    INVOICE_SENT: Final[AuditAction] = "billing.invoice.sent"
    INVOICE_PAID: Final[AuditAction] = "billing.invoice.paid"
    INVOICE_OVERDUE: Final[AuditAction] = "billing.invoice.overdue"
    INVOICE_CREDITED: Final[AuditAction] = "billing.invoice.credited"
    PAYMENT_REGISTERED: Final[AuditAction] = "billing.payment.registered"
