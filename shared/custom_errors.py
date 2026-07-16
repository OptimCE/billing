from core.errors.errors import Error


# ---------------------------------------------------------------------------
# Auth (shared codes injected/asserted by the gateway + security layer)
# ---------------------------------------------------------------------------
class _AuthErrors:
    UNAUTHORIZED = Error(code=1, key="ERRORS.AUTH.UNAUTHORIZED")
    FORBIDDEN = Error(code=2, key="ERRORS.AUTH.FORBIDDEN")
    RATE_LIMITED = Error(code=3, key="ERRORS.AUTH.RATE_LIMITED")
    AUTHORIZATION_MISSING = Error(code=4, key="ERRORS.AUTH.AUTHORIZATION_MISSING")


class _SubscriptionErrors:
    NOT_SUBSCRIBED = Error(code=1003, key="ERRORS.SUBSCRIPTION.NOT_SUBSCRIBED")


# ---------------------------------------------------------------------------
# Billing (2200+). Every key here must have a matching entry in every locale
# file — tests/test_locales.py enforces it.
# ---------------------------------------------------------------------------
class _BillingErrors:
    # -- Tariffs (2200-2209) --
    GET_TARIFFS = Error(code=2200, key="ERRORS.BILLING.GET_TARIFFS")
    TARIFF_NOT_FOUND = Error(code=2201, key="ERRORS.BILLING.TARIFF_NOT_FOUND")
    CREATE_TARIFF = Error(code=2202, key="ERRORS.BILLING.CREATE_TARIFF")
    DELETE_TARIFF = Error(code=2203, key="ERRORS.BILLING.DELETE_TARIFF")
    INVALID_TARIFF = Error(code=2204, key="ERRORS.BILLING.INVALID_TARIFF")

    # -- Billing runs (2210-2219) --
    GET_BILLING_RUNS = Error(code=2210, key="ERRORS.BILLING.GET_BILLING_RUNS")
    BILLING_RUN_NOT_FOUND = Error(code=2211, key="ERRORS.BILLING.BILLING_RUN_NOT_FOUND")
    RUN_ALREADY_EXISTS = Error(code=2212, key="ERRORS.BILLING.RUN_ALREADY_EXISTS")
    NO_CONSUMPTION_DATA = Error(code=2213, key="ERRORS.BILLING.NO_CONSUMPTION_DATA")
    COMMUNITY_BILLING_INFO_INCOMPLETE = Error(
        code=2214, key="ERRORS.BILLING.COMMUNITY_BILLING_INFO_INCOMPLETE"
    )
    DOUBLE_IMPORT_DETECTED = Error(code=2215, key="ERRORS.BILLING.DOUBLE_IMPORT_DETECTED")
    START_BILLING_RUN = Error(code=2216, key="ERRORS.BILLING.START_BILLING_RUN")

    # -- Invoices (2220-2239) --
    GET_INVOICES = Error(code=2220, key="ERRORS.BILLING.GET_INVOICES")
    INVOICE_NOT_FOUND = Error(code=2221, key="ERRORS.BILLING.INVOICE_NOT_FOUND")
    INVOICE_NOT_DRAFT = Error(code=2222, key="ERRORS.BILLING.INVOICE_NOT_DRAFT")
    INVOICE_NOT_ISSUED = Error(code=2223, key="ERRORS.BILLING.INVOICE_NOT_ISSUED")
    NO_BILLING_EMAIL = Error(code=2224, key="ERRORS.BILLING.NO_BILLING_EMAIL")
    NUMBERING_FAILED = Error(code=2225, key="ERRORS.BILLING.NUMBERING_FAILED")
    ISSUE_INVOICE = Error(code=2226, key="ERRORS.BILLING.ISSUE_INVOICE")
    INVOICE_PDF_NOT_READY = Error(code=2227, key="ERRORS.BILLING.INVOICE_PDF_NOT_READY")
    INVOICE_NOT_RENDERABLE = Error(code=2228, key="ERRORS.BILLING.INVOICE_NOT_RENDERABLE")
    INVOICE_PDF_DELETE_FORBIDDEN = Error(
        code=2229, key="ERRORS.BILLING.INVOICE_PDF_DELETE_FORBIDDEN"
    )

    # -- Credit notes / payments (2240-2249) --
    CREDIT_NOTE_TARGET_INVALID = Error(code=2240, key="ERRORS.BILLING.CREDIT_NOTE_TARGET_INVALID")
    REGISTER_PAYMENT = Error(code=2241, key="ERRORS.BILLING.REGISTER_PAYMENT")

    # -- Regime / config (2250+) --
    REGIME_NOT_CONFIGURED = Error(code=2250, key="ERRORS.BILLING.REGIME_NOT_CONFIGURED")


class _Errors:
    auth = _AuthErrors()
    subscription = _SubscriptionErrors()
    billing = _BillingErrors()


errors = _Errors()
