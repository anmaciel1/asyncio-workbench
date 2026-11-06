import asyncio

from workbench.loop_monitor import LoopMonitor
from workbench.workloads import async_wait, blocking_wait


async def _monitor_during(workload):
    async with LoopMonitor(interval=0.01) as m:
        await asyncio.sleep(0.03)  # let a few normal beats happen
        await workload
        await asyncio.sleep(0.03)
    return m


def test_blocking_wait_produces_large_lag():
    m = asyncio.run(_monitor_during(blocking_wait(0.2)))
    assert max(s.lag for s in m.samples) >= 0.15


def test_async_wait_keeps_lag_small():
    m = asyncio.run(_monitor_during(async_wait(0.2)))
    # Loose bound: avoid brittle exact timings on a busy machine.
    assert max(s.lag for s in m.samples) < 0.05
    assert len(m.samples) >= 15


def test_monitor_stops_cleanly():
    async def go():
        m = LoopMonitor(interval=0.01)
        m.start()
        await asyncio.sleep(0.02)
        await m.stop()
        others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        return others

    assert asyncio.run(go()) == []
