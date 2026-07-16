"""Belgian structured communication (OGM / VCS) for invoice payment references.

Format: ``+++ddd/dddd/dddCC+++`` — 10 base digits followed by a 2-digit check,
grouped 3/4/5. The check is the 10-digit base modulo 97; when the remainder is 0
the check digits are 97 (never 00).
"""

from __future__ import annotations

import re

_MAX_BASE = 9_999_999_999  # fits in 10 digits
_DIGITS_ONLY = re.compile(r"\D")


def generate(base: int) -> str:
    """Build a structured communication from a numeric base (0..9_999_999_999)."""
    if not 0 <= base <= _MAX_BASE:
        raise ValueError(f"OGM base must be a 10-digit non-negative integer, got {base}")
    core = f"{base:010d}"
    check = base % 97 or 97
    digits = f"{core}{check:02d}"  # 12 digits total
    return f"+++{digits[0:3]}/{digits[3:7]}/{digits[7:12]}+++"


def validate(structured: str) -> bool:
    """True if ``structured`` is a well-formed OGM with a correct mod-97 check."""
    digits = _DIGITS_ONLY.sub("", structured)
    if len(digits) != 12:
        return False
    base = int(digits[:10])
    check = int(digits[10:])
    return check == (base % 97 or 97)
