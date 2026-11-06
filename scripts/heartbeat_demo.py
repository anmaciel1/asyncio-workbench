"""Milestone 1: scheduling demonstration.

Runs a heartbeat (LoopMonitor) alongside each workload in turn and reports
how late the heartbeat woke up. No HTTP involved yet.

Phases (same duration each):
  idle           heartbeat only (baseline)
  async_wait     repeated `await asyncio.sleep(work)`
  blocking_wait  repeated `time.sleep(work)` inside a coroutine
  cpu_work       repeated fixed-size prime counting (~work seconds)
  json_parse     repeated json.loads of the fake feature-flag config
                 (the OpenAI Habitat / Statsig stall)

Between workload iterations the driver yields with `await asyncio.sleep(gap)`,
which gives the heartbeat a chance to run between stalls.

Usage:
  uv run python scripts/heartbeat_demo.py
  uv run python scripts/heartbeat_demo.py --cpu-n 60000   # frozen CPU size
"""

import argparse
import asyncio
import json
import os
import platform
import sys
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from workbench.flag_config import parse_config, read_config_bytes, write_config
from workbench.loop_monitor import LoopMonitor
from workbench.stats import summarize
from workbench.workloads import async_wait, blocking_wait, calibrate_cpu, cpu_work

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "results"
DEFAULT_CONFIG = ROOT / "data" / "flag_config.json"


async def drive(workload, deadline: float, gap: float) -> int:
    """Run `workload()` repeatedly until `deadline`. Returns iterations done."""
    loop = asyncio.get_running_loop()
    iterations = 0
    while loop.time() < deadline:
        if workload is not None:
            await workload()
            iterations += 1
        # Without this real suspension, a non-yielding workload would hog
        # the loop for the whole phase: awaiting a coroutine that never
        # suspends does NOT give other tasks a turn.
        await asyncio.sleep(gap)
    return iterations


async def run_phase(name, workload, args) -> dict:
    loop = asyncio.get_running_loop()
    cpu0, wall0 = time.process_time(), loop.time()
    async with LoopMonitor(interval=args.interval) as monitor:
        deadline = loop.time() + args.phase_seconds
        driver = asyncio.create_task(drive(workload, deadline, args.gap))
        iterations = await driver
    cpu_s = time.process_time() - cpu0
    wall_s = loop.time() - wall0
    return {
        "name": name,
        "iterations": iterations,
        "wall_s": wall_s,
        "cpu_s": cpu_s,
        "pid": monitor.pid,
        "summary": summarize(monitor.samples),
        "samples": [asdict(s) for s in monitor.samples],
    }


def print_table(phases, args) -> None:
    expected = args.phase_seconds / args.interval
    print(f"\nHeartbeat every {args.interval * 1000:.0f} ms for "
          f"{args.phase_seconds:.1f} s per phase "
          f"(~{expected:.0f} samples if the loop were never late)\n")
    header = (f"{'phase':<14}{'iters':>6}{'samples':>9}{'p50 ms':>9}"
              f"{'p99 ms':>9}{'max ms':>9}{'CPU s':>8}{'CPU %':>7}")
    print(header)
    print("-" * len(header))
    for p in phases:
        s = p["summary"]
        cpu_pct = 100 * p["cpu_s"] / p["wall_s"]
        print(f"{p['name']:<14}{p['iterations']:>6}{s['count']:>9}"
              f"{s['p50_ms']:>9.2f}{s['p99_ms']:>9.2f}{s['max_ms']:>9.2f}"
              f"{p['cpu_s']:>8.2f}{cpu_pct:>6.0f}%")
    print("\nCPU % = this process's CPU time / wall time for the phase.")


async def main(args) -> None:
    work = args.work_ms / 1000
    if not args.config.exists():
        print(f"{args.config} missing; generating it (6000 flags, seed 0)")
        write_config(args.config, num_flags=6_000, seed=0)
    # Read once up front: this phase measures PARSING, not file I/O.
    config_raw = read_config_bytes(args.config)
    phases_def = [
        ("idle", None),
        ("async_wait", lambda: async_wait(work)),
        ("blocking_wait", lambda: blocking_wait(work)),
        ("cpu_work", lambda: cpu_work(args.cpu_n)),
        ("json_parse", lambda: parse_config(config_raw)),
    ]
    phases = []
    for name, workload in phases_def:
        print(f"running phase: {name} ...", flush=True)
        phases.append(await run_phase(name, workload, args))

    print_table(phases, args)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    out_dir = RESULTS_DIR / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact = {
        "run_id": run_id,
        "config": {
            "interval_s": args.interval,
            "phase_seconds": args.phase_seconds,
            "work_ms": args.work_ms,
            "gap_s": args.gap,
            "cpu_n": args.cpu_n,
            "config_path": str(args.config),
            "config_bytes": len(config_raw),
        },
        "environment": {
            "python": sys.version,
            "implementation": platform.python_implementation(),
            "gil_enabled": getattr(sys, "_is_gil_enabled", lambda: True)(),
            "loop": type(asyncio.get_running_loop()).__name__,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "pid": os.getpid(),
        },
        "phases": phases,
    }
    (out_dir / "heartbeat.json").write_text(json.dumps(artifact, indent=2))
    print(f"\nRaw samples saved to {out_dir / 'heartbeat.json'}")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase-seconds", type=float, default=2.0)
    ap.add_argument("--interval", type=float, default=0.01, help="heartbeat interval (s)")
    ap.add_argument("--work-ms", type=float, default=100, help="duration of each wait workload")
    ap.add_argument("--gap", type=float, default=0.05, help="yield between iterations (s)")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                    help="feature-flag JSON for the json_parse phase")
    ap.add_argument("--cpu-n", type=int, default=None,
                    help="frozen prime-count size; calibrated to ~work-ms if omitted")
    args = ap.parse_args()
    if args.cpu_n is None:
        args.cpu_n = calibrate_cpu(args.work_ms / 1000)
        print(f"calibrated --cpu-n {args.cpu_n} (~{args.work_ms:.0f} ms here). "
              f"Pass it explicitly next time to freeze the workload.")
    return args


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
