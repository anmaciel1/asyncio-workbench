"""Small, dependency-free summary statistics for timing samples."""

import math
from collections.abc import Sequence

from workbench.loop_monitor import Sample


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile: the smallest value with at least p% of
    observations at or below it. Returns NaN for an empty input."""
    if not 0 <= p <= 100:
        raise ValueError(f"p must be in [0, 100], got {p}")
    if not values:
        return math.nan
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def summarize(samples: Sequence[Sample]) -> dict[str, float]:
    """Summarize lag samples. Always report the count: a tail percentile
    from a handful of samples is not a stable estimate."""
    lags = [s.lag for s in samples]
    span = samples[-1].actual - samples[0].intended if samples else 0.0
    return {
        "count": len(lags),
        "span_s": span,
        "p50_ms": percentile(lags, 50) * 1000,
        "p95_ms": percentile(lags, 95) * 1000,
        "p99_ms": percentile(lags, 99) * 1000,
        "max_ms": (max(lags) * 1000) if lags else math.nan,
    }
