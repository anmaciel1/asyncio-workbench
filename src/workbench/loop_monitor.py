"""Heartbeat monitor that measures event-loop scheduling delay (lag).

How it works: a background task repeatedly asks to sleep for ``interval``.
It notes when it *intended* to wake up and when it *actually* woke up. The
difference is the lag: how long the loop took to get back to this task.

Two things to keep in mind:

* Lag is a scheduling-delay signal, not a CPU percentage. A loop blocked by
  ``time.sleep`` shows lag while using almost no CPU.
* During a stall the monitor cannot run either. We do NOT invent the missing
  samples. A single late sample stands for the whole stall, and the fewer
  samples in that window are themselves evidence.
"""

import asyncio
import os
from dataclasses import dataclass, field


def compute_lag(intended: float, actual: float) -> float:
    """Lag = actual wake-up time minus intended wake-up time (seconds)."""
    return actual - intended


@dataclass(frozen=True)
class Sample:
    sample      : int    # sample number, to spot gaps
    intended    : float  # loop.time() we asked to wake at
    actual      : float  # loop.time() we actually woke at
    lag         : float  # actual - intended


@dataclass
class LoopMonitor:
    """Records heartbeat samples while running. Use as ``async with``."""

    interval: float = 0.01
    samples: list[Sample] = field(default_factory=list)
    pid: int = field(default_factory=os.getpid)
    _task: asyncio.Task | None = field(default=None, repr=False)

    async def _run(self) -> None:
        # loop.time() is a monotonic clock: it never jumps when the wall
        # clock changes, so it is safe for measuring durations.
        loop = asyncio.get_running_loop()
        sample = 0
        while True:
            intended = loop.time() + self.interval
            await asyncio.sleep(self.interval)
            actual = loop.time()
            self.samples.append(
                Sample(sample, intended, actual, compute_lag(intended, actual))
            )
            sample += 1

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("monitor already started")
        # Keep a reference: the loop only holds weak refs to tasks, so an
        # unreferenced task can be garbage-collected mid-run.
        self._task = asyncio.create_task(self._run(), name="loop-monitor")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task  # wait for the cancellation to actually finish
        except asyncio.CancelledError:
            pass
        self._task = None

    async def __aenter__(self) -> "LoopMonitor":
        self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()
