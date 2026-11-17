"""Periodic config poller: the OpenAI incident in miniature.

Every ``interval`` seconds, each server process:

1. "downloads" the config (reads the file in a worker thread, which is I/O
   and does not hold the event loop), then
2. parses it with ``json.loads`` ON THE EVENT LOOP, which holds the loop for
   the whole parse. Every request that arrives during the parse has to wait.

Without jitter, every process wakes on the same wall-clock boundary (like
cron's "every minute on the minute"). With 8 processes, all 8 parse at the
same moment, so for ~100 ms there is no free process left to answer anyone.
Jitter adds a random 0..``jitter`` second offset per process and per cycle,
which spreads the parses out.
"""

import asyncio
import json
import os
import random
import time
from pathlib import Path

from workbench.flag_config import parse_config, read_config_bytes


def next_delay(now: float, interval: float, jitter: float, rng: random.Random) -> float:
    """Seconds to sleep until the next poll.

    ``now`` is wall-clock time (time.time()), shared by all processes, which
    is why processes without jitter all wake together.
    """
    next_tick = (now // interval + 1) * interval
    return next_tick - now + rng.uniform(0, jitter)


async def poll_config(
    config_path: Path,
    interval: float,
    jitter: float,
    events: list[dict],
    log_path: Path | None = None,
) -> None:
    rng = random.Random()  # seeded from the OS: different in every process
    pid = os.getpid()
    while True:
        await asyncio.sleep(next_delay(time.time(), interval, jitter, rng))

        # The "download". Reading a file is I/O, so we move it off the loop.
        raw = await asyncio.to_thread(read_config_bytes, config_path)

        # The stall. parse_config is `async def` but has no `await` inside,
        # so the loop cannot serve anything else until it returns.
        start_wall = time.time()
        t0 = time.perf_counter()
        rules = await parse_config(raw)
        duration = time.perf_counter() - t0

        event = {
            "pid": pid,
            "start_wall": start_wall,
            "end_wall": start_wall + duration,
            "duration_ms": duration * 1000,
            "rules": rules,
        }
        events.append(event)
        if log_path is not None:
            # One short line appended per parse, so every process can share
            # one file. This tiny write is the only other work on the loop.
            with open(log_path, "a") as f:
                f.write(json.dumps(event) + "\n")
