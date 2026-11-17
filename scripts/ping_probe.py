"""Send /ping at a fixed rate and line the latencies up with config parses.

This is an OPEN load model: requests go out on a fixed schedule, whether or
not earlier ones have come back. A stalled server cannot slow the probe
down, so the stalls show up in the latencies instead of being hidden.

Run the server with the poller on and a run dir, e.g. (8 processes, all
parsing together every 5 s):

  WORKBENCH_RUN_DIR=results/sync WORKBENCH_POLL_INTERVAL=5 \\
    uv run uvicorn workbench.app:create_app --factory --loop asyncio --workers 8

then, in another terminal:

  uv run python scripts/ping_probe.py --run-dir results/sync
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx

from workbench.stats import percentile


async def send(client: httpx.AsyncClient, url: str, planned_wall: float) -> dict:
    sent_wall = time.time()
    t0 = time.perf_counter()
    try:
        r = await client.get(url)
        status, pid = r.status_code, r.json().get("pid")
    except httpx.HTTPError as e:
        status, pid = type(e).__name__, None
    return {
        "planned_wall": planned_wall,
        "sent_wall": sent_wall,
        "latency_ms": (time.perf_counter() - t0) * 1000,
        "status": status,
        "pid": pid,
    }


async def run(url: str, rate: float, seconds: float) -> list[dict]:
    loop = asyncio.get_running_loop()
    # No keep-alive: every ping opens a new connection, and any process that
    # is free can accept it. (With keep-alive, a connection stays tied to one
    # process, busy or not.)
    limits = httpx.Limits(max_keepalive_connections=0)
    async with httpx.AsyncClient(limits=limits, timeout=10) as client:
        start_loop, start_wall = loop.time(), time.time()
        tasks = []
        for i in range(int(rate * seconds)):
            offset = i / rate
            await asyncio.sleep(max(0.0, start_loop + offset - loop.time()))
            tasks.append(asyncio.create_task(send(client, url, start_wall + offset)))
        return list(await asyncio.gather(*tasks))


def load_parses(run_dir: Path | None, t_start: float, t_end: float) -> list[dict]:
    if run_dir is None or not (run_dir / "parses.jsonl").exists():
        return []
    parses = [json.loads(line) for line in (run_dir / "parses.jsonl").read_text().splitlines() if line]
    return [p for p in parses if p["end_wall"] >= t_start and p["start_wall"] <= t_end]


def during_parse(ping: dict, parses: list[dict]) -> bool:
    return any(p["start_wall"] <= ping["sent_wall"] <= p["end_wall"] for p in parses)


def stats_row(label: str, pings: list[dict]) -> str:
    lat = [p["latency_ms"] for p in pings]
    if not lat:
        return f"  {label:<16} {0:>6}"
    return (f"  {label:<16} {len(lat):>6} {percentile(lat, 50):>8.1f} "
            f"{percentile(lat, 99):>8.1f} {max(lat):>8.1f}")


def print_report(pings: list[dict], parses: list[dict], bucket_s: float) -> None:
    t0 = pings[0]["planned_wall"]
    ok = [p for p in pings if p["status"] == 200]
    print(f"\npings sent: {len(pings)}   ok: {len(ok)}   "
          f"processes answering: {len({p['pid'] for p in ok})}   "
          f"parses seen: {len(parses)} by {len({p['pid'] for p in parses})} processes")

    print(f"\n  {'':<16} {'count':>6} {'p50 ms':>8} {'p99 ms':>8} {'max ms':>8}")
    print(stats_row("all pings", ok))
    if parses:
        print(stats_row("during a parse", [p for p in ok if during_parse(p, parses)]))
        print(stats_row("outside parses", [p for p in ok if not during_parse(p, parses)]))

    # Timeline: one row per bucket. How many processes were parsing, and how
    # slow was the slowest ping sent in that bucket?
    print(f"\n  {'t (s)':>6}  {'parsing':>7}  {'max ping ms':>11}")
    n_buckets = int((pings[-1]["planned_wall"] - t0) / bucket_s) + 1
    for b in range(n_buckets):
        lo, hi = t0 + b * bucket_s, t0 + (b + 1) * bucket_s
        in_bucket = [p["latency_ms"] for p in ok if lo <= p["sent_wall"] < hi]
        busy = {p["pid"] for p in parses if p["start_wall"] < hi and p["end_wall"] >= lo}
        worst = max(in_bucket, default=0.0)
        bar = "█" * min(60, int(worst / 5))
        print(f"  {b * bucket_s:>6.1f}  {len(busy):>7}  {worst:>11.1f}  {bar}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8000/ping")
    ap.add_argument("--rate", type=float, default=50, help="pings per second")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--run-dir", type=Path, help="server's WORKBENCH_RUN_DIR (to read parses.jsonl)")
    ap.add_argument("--bucket", type=float, default=0.5, help="timeline row size in seconds")
    args = ap.parse_args()

    print(f"sending {args.rate:g} pings/s to {args.url} for {args.seconds:g} s ...")
    pings = asyncio.run(run(args.url, args.rate, args.seconds))
    parses = load_parses(args.run_dir, pings[0]["planned_wall"], pings[-1]["sent_wall"] + 1)
    print_report(pings, parses, args.bucket)

    if args.run_dir is not None:
        out = args.run_dir / "pings.json"
        out.write_text(json.dumps({"rate": args.rate, "seconds": args.seconds, "pings": pings}, indent=2))
        print(f"\nsaved {out}")


if __name__ == "__main__":
    main()
