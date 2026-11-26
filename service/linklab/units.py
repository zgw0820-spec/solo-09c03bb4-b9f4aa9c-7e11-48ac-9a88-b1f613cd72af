"""Rate unit helpers.

Inside the backend every rate is an int measured in kbit/s.
The web UI uses Mbit/s (float), and tc argv uses ``<n>kbit``.
"""
from __future__ import annotations

import math
from typing import Any

# Hard sanity bounds for the lab (64 kbit/s .. 10 Gbit/s).
MIN_KBPS = 64
MAX_KBPS = 10_000_000


def mbps_to_kbps(value: Any) -> int:
    """Convert a Mbit/s form value to whole kbit/s."""
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"速率不是数字: {value!r}") from exc
    if not math.isfinite(f):
        raise ValueError("速率必须是有限数值")
    return int(round(f * 1000.0))


def kbps_to_mbps(kbps: int | float) -> float:
    return round(float(kbps) / 1000.0, 3)


def kbps_to_tc(kbps: int) -> str:
    if not isinstance(kbps, int) or kbps <= 0:
        raise ValueError(f"tc 速率必须是正整数 kbit/s, got {kbps!r}")
    return f"{kbps}kbit"


def parse_offered_rate(value: str | int | float | None) -> int:
    """Parse a sender offered rate into bit/s.

    Accepts a number (interpreted as Mbit/s for the web API convenience)
    or strings like ``"12mbit"`` / ``"500kbit"`` / ``"2mbps"``.
    ``0`` means unrestricted flood mode.
    """
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return int(float(value) * 1_000_000)
    s = str(value).strip().lower()
    if s in ("0", "max", "flood"):
        return 0
    mult = 1
    for suffix, factor in (
        ("gbit", 1_000_000_000),
        ("gbps", 1_000_000_000),
        ("mbit", 1_000_000),
        ("mbps", 1_000_000),
        ("kbit", 1_000),
        ("kbps", 1_000),
    ):
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
            mult = factor
            break
    else:
        # Bare number: treat as Mbit/s for short form convenience.
        mult = 1_000_000
    try:
        return int(float(s) * mult)
    except ValueError as exc:
        raise ValueError(f"无法解析发包速率: {value!r}") from exc
