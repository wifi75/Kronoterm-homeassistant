"""Temperature parsing helpers for Kronoterm API values."""

from __future__ import annotations

import math
from typing import Any


def parse_temperature(
    value: Any,
    *,
    minimum: float = -50.0,
    maximum: float = 120.0,
) -> float | None:
    """Return a physical temperature or None for API error/sentinel values."""
    if value is None or value in ("", "unknown", "unavailable"):
        return None

    try:
        temperature = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(temperature):
        return None
    if temperature < minimum or temperature > maximum:
        return None
    return temperature
