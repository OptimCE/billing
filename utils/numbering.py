"""Render a gapless sequence value into a regime's invoice number format."""

from __future__ import annotations


def format_number(number_format: str, *, prefix: str, year: int, seq: int) -> str:
    """Apply a ``str.format`` template like ``{prefix}-{year}-{seq:05d}``."""
    return number_format.format(prefix=prefix, year=year, seq=seq)
