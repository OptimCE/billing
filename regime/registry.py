"""Regime registry + startup parity assertion.

The registry maps a regulator code to its strategy. At startup, parity is
asserted against the shared regulator registry: every ACTIVE regulator must have
exactly one registered strategy (with a config block), and no strategy may exist
for an inactive/unknown regulator. A mismatch fails loudly at boot rather than at
the first billing run.
"""

from __future__ import annotations

from collections.abc import Iterable

from regime.base import BillingRegime
from regime.config_loader import load_billing_regimes, load_regulators
from regime.cwape import CwapeWalloniaRegime


class RegimeConfigError(RuntimeError):
    """Raised when regime configuration is missing or inconsistent."""


# Regulator code → strategy class. Adding a region = one entry here + a config
# block in billing_regimes.json + an active:true row in reference/regulators.json.
_STRATEGY_CLASSES: dict[str, type] = {
    "BE-WAL-CWAPE": CwapeWalloniaRegime,
}


class RegimeRegistry:
    def __init__(self, by_code: dict[str, BillingRegime]) -> None:
        self._by_code = by_code

    def get_for(self, regulator_code: str) -> BillingRegime:
        try:
            return self._by_code[regulator_code]
        except KeyError:
            raise RegimeConfigError(
                f"No billing regime registered for regulator '{regulator_code}'"
            ) from None

    def codes(self) -> set[str]:
        return set(self._by_code)


def build_registry(regime_configs: dict[str, dict]) -> RegimeRegistry:
    """Instantiate a strategy for each code that has BOTH a class and a config block."""
    by_code: dict[str, BillingRegime] = {}
    for code, strategy_cls in _STRATEGY_CLASSES.items():
        if code in regime_configs:
            by_code[code] = strategy_cls(code, regime_configs[code])
    return RegimeRegistry(by_code)


def assert_parity(regulators: Iterable[dict], registry: RegimeRegistry) -> None:
    active = {r["code"] for r in regulators if r.get("active")}
    registered = registry.codes()
    missing = active - registered  # active regulator with no strategy/config
    extra = registered - active  # strategy for an inactive/unknown regulator
    if missing or extra:
        raise RegimeConfigError(
            "Regime parity mismatch: "
            f"active-without-strategy={sorted(missing)}, "
            f"strategy-without-active-regulator={sorted(extra)}"
        )


_registry: RegimeRegistry | None = None


def get_registry() -> RegimeRegistry:
    """Return the process-wide registry, building it on first use."""
    global _registry
    if _registry is None:
        _registry = build_registry(load_billing_regimes())
    return _registry


def assert_regime_parity() -> RegimeRegistry:
    """Build the registry and assert it matches the active regulators. Call at startup."""
    registry = get_registry()
    assert_parity(load_regulators(), registry)
    return registry
