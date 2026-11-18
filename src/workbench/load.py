"""Send HTTP requests on a fixed schedule (an OPEN load model).

Requests go out at planned times, whether or not earlier ones have come
back. A stalled server cannot slow the sender down, so stalls show up as
latency instead of as fewer requests sent.

Used by scripts/ping_probe.py and by the runner (workbench.runner).
"""

import asyncio
import time

import httpx


async def send(client: httpx.AsyncClient, url: str, params: dict, planned_wall: float,
               read_pid: bool) -> dict:
    sent_wall = time.time()
    t0 = time.perf_counter()
    pid = None
    try:
        r = await client.get(url, params=params)
        status = r.status_code
        if read_pid and status == 200:
            # Only for small responses like /ping. Parsing a large payload
            # here would make the SENDER's own event loop stall.
            pid = r.json().get("pid")
    except httpx.HTTPError as e:
        status = type(e).__name__
    return {
        "planned_wall": planned_wall,
        "sent_wall": sent_wall,
        "latency_ms": (time.perf_counter() - t0) * 1000,
        "status": status,
        "pid": pid,
    }


async def open_loop(
    url: str,
    rate: float,
    seconds: float,
    params: dict | None = None,
    keep_alive: bool = False,
    timeout_s: float = 10.0,
    read_pid: bool = True,
) -> list[dict]:
    """Send ``rate`` requests per second for ``seconds``; return one record each."""
    loop = asyncio.get_running_loop()
    # keep_alive=False: every request opens a new connection, so any free
    # server process can accept it. With keep-alive, a connection stays tied
    # to one process, busy or not.
    limits = httpx.Limits(max_keepalive_connections=None if keep_alive else 0)
    async with httpx.AsyncClient(limits=limits, timeout=timeout_s) as client:
        start_loop, start_wall = loop.time(), time.time()
        tasks = []
        for i in range(int(rate * seconds)):
            offset = i / rate
            await asyncio.sleep(max(0.0, start_loop + offset - loop.time()))
            tasks.append(asyncio.create_task(
                send(client, url, params or {}, start_wall + offset, read_pid)))
        return list(await asyncio.gather(*tasks))
