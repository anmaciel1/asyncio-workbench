"""Controlled workloads for scheduling experiments.

Each workload is an ``async def`` on purpose: we want to see what happens
when the code runs *on the event loop's thread*. The three cases differ in
whether they give control back to the loop and whether they use CPU.

| Workload       | Yields to loop? | Uses CPU? |
| -------------- | --------------- | --------- |
| async_wait     | yes             | no        |
| blocking_wait  | no              | no        |
| cpu_work       | no              | yes       |
"""

import asyncio
import time


async def async_wait(seconds: float) -> None:
    """
    Nonblocking wait

    Simulates waiting on something like a dependency or a db reply. It is not a
    network benchmark.
    """
    await asyncio.sleep(seconds)


async def blocking_wait(seconds: float) -> None:
    """
    Blocking wait: 

    ``time.sleep`` never returns control to the event loop, so no other
    task (including the heartbeat) can run until it finishes.
    """
    time.sleep(seconds)


# ------------------------

'''
These 2 functions are stand ins for a problem described by openAI, in their blog:
    "Rapidly scaling online storage to serve over 1 billion ChatGPT users"

where asyncio delay was coming from a periodic JSON parse. This came from a Statsig config
feature to poll for refreshed configs every minute with no jitter. Somewhere else, an architecture
change "made to run up to 8 Python processes per pod to push higher CPU usage and provide lower latencies.
Combined, this meant that every minute each pod would have some moment where all of its workers stalled
processing in-flight requests and instead would spend their CPU cycles parsing a giant configuration file."

I also made a config file generator and parser to accurately show the problem
'''
def count_primes(n: int) -> int:
    """
    Count primes

    the amount of work is set by ``n``
    rather than by elapsed time, so every run does the same work and
    the answer can be checked.
    """
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    count = 0
    for candidate in range(2, n):
        is_prime = True
        divisor = 2
        
        while divisor * divisor <= candidate:
            if candidate % divisor == 0:
                is_prime = False
                break
            divisor += 1
        
        if is_prime:
            count += 1
    
    return count


async def cpu_work(n: int) -> int:
    """
    CPU work on the loop:

    There is no ``await`` inside, so the loop cannot switch tasks until the
    calculation ends.
    """
    return count_primes(n)

# ------------------------
def calibrate_cpu(target_seconds: float, start_n: int = 1_000) -> int:
    """
    Pick an ``n`` that takes about ``target_seconds`` on this machine.

    Use this once, then FREEZE the result for comparisons. Calibrating
    before every run would let the amount of work drift between runs.
    """
    n = start_n
    
    while True:
        t0 = time.perf_counter()
        count_primes(n)
        elapsed = time.perf_counter() - t0
        
        if elapsed >= target_seconds:
            return n
        # Grow toward the target (cost grows a bit faster than linearly).
        
        scale = target_seconds / max(elapsed, 1e-4)
        n = int(n * min(max(scale, 1.2), 4))
