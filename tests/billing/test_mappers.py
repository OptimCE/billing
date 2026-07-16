"""Unit tests for docgen-data mappers (pure, no DB)."""

from api.billing.mappers import _format_address
from ports.crm_core import CommunityIdentity


def _identity(**over: object) -> CommunityIdentity:
    base: dict[str, object] = {
        "id": 1,
        "regulator": "BE-WAL-CWAPE",
        "legal_name": "ACME ASBL",
        "vat_number": None,
        "iban": "BE68539007547034",
        "account_holder_name": None,
        "street": "Rue de la Loi",
        "number": "16",
        "postcode": "1000",
        "city": "Bruxelles",
        "supplement": None,
    }
    base.update(over)
    return CommunityIdentity(**base)  # type: ignore[arg-type]


def test_format_address_normal() -> None:
    assert _format_address(_identity()) == "Rue de la Loi 16, 1000 Bruxelles"


def test_format_address_tolerates_int_number() -> None:
    # Defensive guard: the CRM `address.number` column is an integer, so an int can
    # reach the formatter. It must not raise a TypeError — that used to dead-letter
    # the whole invoice-issue message and block PDF generation entirely.
    assert _format_address(_identity(number=16)) == "Rue de la Loi 16, 1000 Bruxelles"


def test_format_address_none_returns_none() -> None:
    assert _format_address(None) is None
