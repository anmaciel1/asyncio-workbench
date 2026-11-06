import asyncio

import pytest

from workbench.workloads import count_primes, cpu_work


@pytest.mark.parametrize("n, expected", [(0, 0), (2, 0), (3, 1), (10, 4), (100, 25), (10_000, 1229)])
def test_count_primes_known_values(n, expected):
    assert count_primes(n) == expected


def test_cpu_work_returns_same_answer_as_sync():
    assert asyncio.run(cpu_work(10_000)) == 1229


def test_count_primes_rejects_negative():
    with pytest.raises(ValueError):
        count_primes(-1)
