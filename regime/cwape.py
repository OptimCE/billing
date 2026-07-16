"""CwapeWalloniaRegime — the sole concrete regime in v1 (Wallonia / CWaPE).

Pure: all declarative attributes are injected as a config dict (no I/O here), so
the regime is trivially unit-testable and a new region is just a new config
block + a new strategy class registered in the registry.
"""

from __future__ import annotations

from decimal import Decimal

from shared.const import InvoiceType, Measure
from utils.numbering import format_number

_MEASURE_BY_NAME = {"SHARED": Measure.SHARED, "INJ_SHARED": Measure.INJ_SHARED}
_TYPE_NAME = {
    InvoiceType.INVOICE: "INVOICE",
    InvoiceType.CREDIT_NOTE: "CREDIT_NOTE",
    InvoiceType.PRODUCER_STATEMENT: "PRODUCER_STATEMENT",
}


class CwapeWalloniaRegime:
    def __init__(self, code: str, config: dict) -> None:
        self.code = code
        self._config = config

    def billable_measures(self) -> dict[str, list[Measure]]:
        raw = self._config["billable_measures"]
        return {side: [_MEASURE_BY_NAME[m] for m in measures] for side, measures in raw.items()}

    def vat_rate(self, *, member_type: int, direction: int, social_rate: bool) -> Decimal:
        # CWaPE v1: a single configured rate. There is no legal "tarif social" on
        # shared energy (spec §2.5), so social_rate does not lower VAT. The actual
        # rate / any exemptions are placeholders pending fiscal sign-off.
        return Decimal(str(self._config["vat_rate"]))

    def due_days(self) -> int:
        return int(self._config["due_days"])

    def series_prefix(self, doc_type: InvoiceType) -> str:
        return str(self._config["series_prefixes"][_TYPE_NAME[InvoiceType(doc_type)]])

    def format_number(self, *, doc_type: InvoiceType, year: int, seq: int) -> str:
        return format_number(
            self._config["number_format"],
            prefix=self.series_prefix(doc_type),
            year=year,
            seq=seq,
        )

    def legal_mentions(self, *, locale: str) -> list[str]:
        mentions: dict[str, list[str]] = self._config.get("legal_mentions", {})
        if locale in mentions:
            return list(mentions[locale])
        # Fall back to the same language (fr-BE → any fr-*), else empty.
        lang = locale.split("-")[0]
        for key, value in mentions.items():
            if key.split("-")[0] == lang:
                return list(value)
        return []

    def producer_mode(self) -> str:
        return str(self._config.get("producer_mode", "PRODUCER_STATEMENT"))
