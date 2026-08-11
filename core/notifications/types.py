class NotificationTypes:
    """Notification ``type`` keys this service publishes.

    ``<feature>.<event>`` strings, matching crm-backend's free-form taxonomy.
    The frontend localises the displayed text from the key
    (``NOTIFICATIONS.TYPES.<type>.title``); the backend stores only key + data.

    Every key added here also needs an entry in
    ``crm-frontend/src/app/features/notifications/services/notification-type.registry.ts``
    and a title in all four ``crm-frontend/src/assets/i18n/*.json`` files, or it
    renders to the user as a raw i18n key with no error anywhere.

    This is the ONLY per-service module in ``core/notifications``; the other four
    are byte-identical across producers so Phase 2's extraction is a move rather
    than a rewrite.
    """

    INVOICE_ISSUED = "invoice.issued"
    INVOICE_OVERDUE = "invoice.overdue"
    BILLING_RUN_COMPLETED = "billing_run.completed"
