import math

from workbench.loop_monitor import Sample, compute_lag
from workbench.stats import percentile, summarize


def test_compute_lag_with_controlled_timestamps():
    assert compute_lag(intended=10.0, actual=10.25) == 0.25


def test_percentile_nearest_rank():
    values = list(range(1, 101))  # 1..100
    assert percentile(values, 50) == 50
    assert percentile(values, 99) == 99
    assert percentile(values, 100) == 100
    assert math.isnan(percentile([], 50))


def test_summarize_keeps_single_stall_as_one_sample():
    # Three on-time beats, then one beat 200 ms late (a stall). The stall
    # is NOT backfilled with extra samples.
    samples = [
        Sample(0, 1.00, 1.001, 0.001),
        Sample(1, 1.011, 1.012, 0.001),
        Sample(2, 1.022, 1.023, 0.001),
        Sample(3, 1.033, 1.233, 0.200),
    ]
    s = summarize(samples)
    assert s["count"] == 4
    assert math.isclose(s["max_ms"], 200.0)
    assert math.isclose(s["p50_ms"], 1.0)
