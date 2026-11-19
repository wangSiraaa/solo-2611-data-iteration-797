"""Strict rational parsing shared by the JSON-facing layers.

Floats are rejected on purpose: JSON ``0.1`` would become a binary float and
silently introduce a rounding error.  Accepted forms are JSON integers, strings
like ``"20/3"``, and ``{"n": .., "d": ..}`` objects.  All output uses the
``{"n","d"}`` canonical form.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from .errors import ValidationError


def parse_frac(node: Any, field: str = "value") -> Fraction:
    if isinstance(node, bool):
        raise ValidationError(f"{field}: booleans are not rational numbers")
    if isinstance(node, float):
        raise ValidationError(
            f"{field}: floats are not accepted; use an int, a string, or {{'n','d'}}"
        )
    try:
        if isinstance(node, dict):
            if "n" not in node or "d" not in node:
                raise ValidationError(f"{field}: fraction object needs 'n' and 'd'")
            d = int(node["d"])
            if d == 0:
                raise ValidationError(f"{field}: zero denominator")
            return Fraction(int(node["n"]), d)
        if isinstance(node, int):
            return Fraction(node)
        if isinstance(node, str):
            return Fraction(node)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValidationError(f"{field}: not a rational number") from exc
    raise ValidationError(f"{field}: unsupported rational representation")
