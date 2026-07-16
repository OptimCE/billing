"""BillingRegime — the region/country strategy axis.

A regime carries the RULES (billable measures, VAT, due days, number format,
legal mentions) but NOT price: prices are community-set free fields in the
tariff table. Declarative attributes come from the billing-local regime config
(regime/billing_regimes.json) keyed by the regulator code; behaviour is code
keyed by the same code (see registry). v1 ships one concrete regime, CWaPE.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from shared.const import InvoiceType, Measure


class BillingRegime(Protocol):
    code: str

    def billable_measures(self) -> dict[str, list[Measure]]:
        """Billable measures per side: {'consumer': [SHARED], 'producer': [INJ_SHARED]}."""
        ...

    def vat_rate(self, *, member_type: int, direction: int, social_rate: bool) -> Decimal:
        """VAT rate applied to a line's net, as a Decimal fraction (e.g. 0.21)."""
        ...

    def due_days(self) -> int:
        """Default payment term in days added to the issue date."""
        ...

    def series_prefix(self, doc_type: InvoiceType) -> str:
        """Document-series prefix (e.g. INVOICE→'F'), used in the number + sequence key."""
        ...

    def format_number(self, *, doc_type: InvoiceType, year: int, seq: int) -> str:
        """Render the gapless invoice number for a document type."""
        ...

    def legal_mentions(self, *, locale: str) -> list[str]:
        """Regulatory footer lines for the invoice in the given locale."""
        ...

    def producer_mode(self) -> str:
        """How producer remuneration is represented (v1: 'PRODUCER_STATEMENT')."""
        ...
