"""Load the shared regulator registry and the billing-local regime config.

- Regulators (which regulators exist / are active) come from the SHARED
  reference/regulators.json, consumed read-only via REGULATORS_CONFIG_PATH.
- Billing declarative attributes (VAT, due days, number format, legal mentions)
  come from the billing-LOCAL regime/billing_regimes.json, keyed by the same
  regulator code, so billing's fiscal policy never leaks into the shared file.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.config import settings

# billing/regime/config_loader.py → billing/ → monorepo/
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_REGULATORS = _REPO_ROOT / "reference" / "regulators.json"
_DEFAULT_REGIMES = Path(__file__).resolve().parent / "billing_regimes.json"


def _regulators_path() -> Path:
    return (
        Path(settings.REGULATORS_CONFIG_PATH)
        if settings.REGULATORS_CONFIG_PATH
        else _DEFAULT_REGULATORS
    )


def _regimes_path() -> Path:
    return (
        Path(settings.BILLING_REGIMES_CONFIG_PATH)
        if settings.BILLING_REGIMES_CONFIG_PATH
        else _DEFAULT_REGIMES
    )


def load_regulators() -> list[dict]:
    """Return the shared regulator registry entries ({code, label, region, country, active})."""
    with open(_regulators_path(), encoding="utf-8") as fh:
        data: list[dict] = json.load(fh)
        return data


def load_billing_regimes() -> dict[str, dict]:
    """Return the billing declarative config keyed by regulator code."""
    with open(_regimes_path(), encoding="utf-8") as fh:
        data: dict[str, dict] = json.load(fh)
        return data
