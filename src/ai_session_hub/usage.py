from __future__ import annotations

import math
from dataclasses import dataclass


def counter(value: object) -> int | None:
    """Accept native nonnegative integral counters, not bools or coerced strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        return None
    return int(value) if 0 <= value <= 9_223_372_036_854_775_807 else None


def amount(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


@dataclass(frozen=True)
class PricingRule:
    model: str
    provider: str | None = None
    input_per_million: float | None = None
    output_per_million: float | None = None
    cache_read_per_million: float | None = None
    cache_write_per_million: float | None = None


@dataclass(frozen=True)
class UsageSummary:
    total_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None
    records: int = 0
    known_token_records: int = 0
    priced_records: int = 0
    estimated_records: int = 0
    session_count: int = 0
    unknown_sessions: int = 0
    message_count: int = 0
    models: tuple[str, ...] = ()


@dataclass(frozen=True)
class UsageGroup:
    key: str
    label: str
    summary: UsageSummary


@dataclass(frozen=True)
class DailyUsage:
    day: str  # local calendar ISO date
    total_tokens: int
    cost_usd: float | None = None


@dataclass(frozen=True)
class AnalyticsSnapshot:
    summary: UsageSummary = UsageSummary()
    models: tuple[UsageGroup, ...] = ()
    tools: tuple[UsageGroup, ...] = ()
    projects: tuple[UsageGroup, ...] = ()
    days: tuple[DailyUsage, ...] = ()
    unattributed_tokens: int = 0
