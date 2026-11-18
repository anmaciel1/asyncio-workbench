"""Run scenario files: start the server, warm up, measure, save everything.

    uv run python -m workbench.runner scenarios/cpu_work.toml
    uv run python -m workbench.runner scenarios/*.toml        # several, then a comparison table

For each run, the runner:

1. creates a results folder and saves the scenario and the environment,
2. starts uvicorn as a child process with the scenario's settings,
3. waits until /ping answers (readiness),
4. warm-up: sends traffic and throws the results away,
5. measurement: sends traffic and keeps the results,
6. stops the server like Ctrl-C, so every worker saves its loop samples,
7. keeps only the data from the measurement window and writes a summary.
"""

import argparse
import asyncio
import json
import os
import platform
import resource
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime
from importlib import metadata
from pathlib import Path

import httpx

from workbench.flag_config import write_config
from workbench.load import open_loop
from workbench.loop_monitor import Sample
from workbench.scenario import Scenario, ScenarioError, load_scenario
from workbench.stats import percentile, summarize

ROOT = Path(__file__).resolve().parents[2]
CONFIG_SEED = 0


# ---- setup -----------------------------------------------------------------

def free_port() -> int:
    """Ask the OS for an unused port, so a run never collides with a server
    you already have open on 8000."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def git_info() -> dict:
    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                                  text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def environment() -> dict:
    packages = {}
    for name in ("fastapi", "starlette", "uvicorn", "httpx", "anyio"):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "python": sys.version,
        "gil_enabled": getattr(sys, "_is_gil_enabled", lambda: True)(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "packages": packages,
        "git": git_info(),
    }


def flag_config_for(flags: int, data_dir: Path) -> Path:
    """One config file per size, generated once and reused (same seed, same file)."""
    path = data_dir / f"flag_config_{flags}_seed{CONFIG_SEED}.json"
    if not path.exists():
        write_config(path, num_flags=flags, seed=CONFIG_SEED)
    return path


# ---- the server process ----------------------------------------------------

def start_server(sc: Scenario, run_dir: Path, port: int, config_path: Path) -> subprocess.Popen:
    # Start from a clean environment: a WORKBENCH_* variable left in your shell
    # must not leak into a run and silently change it.
    env = {k: v for k, v in os.environ.items() if not k.startswith("WORKBENCH_")}
    env.update({
        "WORKBENCH_RUN_DIR": str(run_dir),
        "WORKBENCH_CONFIG": str(config_path),
        "WORKBENCH_LOOP_INTERVAL": str(sc.server.loop_interval),
        "WORKBENCH_POLL_JITTER": str(sc.server.poll_jitter),
    })
    if sc.server.poll_interval > 0:
        env["WORKBENCH_POLL_INTERVAL"] = str(sc.server.poll_interval)
    cmd = [sys.executable, "-m", "uvicorn", "workbench.app:create_app", "--factory",
           "--host", "127.0.0.1", "--port", str(port),
           "--loop", sc.server.loop, "--workers", str(sc.server.workers),
           "--log-level", "warning"]
    log = open(run_dir / "server.log", "w")
    return subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)


def log_tail(run_dir: Path, lines: int = 15) -> str:
    text = (run_dir / "server.log").read_text(errors="replace").splitlines()
    return "\n".join(text[-lines:])


def wait_until_ready(proc: subprocess.Popen, base: str, workers: int, run_dir: Path,
                     timeout_s: float = 30) -> dict:
    """Poll /ping until the server answers. With several workers, keep going
    briefly to see how many different processes answer."""
    t0 = time.monotonic()
    pids: set[int] = set()
    with httpx.Client(timeout=1, limits=httpx.Limits(max_keepalive_connections=0)) as client:
        while time.monotonic() - t0 < timeout_s:
            if proc.poll() is not None:
                raise RuntimeError(f"server exited during startup:\n{log_tail(run_dir)}")
            try:
                r = client.get(base + "/ping")
                if r.status_code == 200:
                    pids.add(r.json()["pid"])
                    if len(pids) >= workers or time.monotonic() - t0 > 10:
                        break
                    continue
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError(f"server not ready after {timeout_s} s:\n{log_tail(run_dir)}")
    return {"ready_s": time.monotonic() - t0, "workers_seen": len(pids)}


def stop_server(proc: subprocess.Popen, timeout_s: float = 20) -> bool:
    """Stop like Ctrl-C (SIGINT), so lifespan shutdown runs and saves the loop
    samples. Returns True if the server stopped by itself in time."""
    if proc.poll() is not None:
        return False
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout_s)
        return True
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        return False


# ---- traffic ---------------------------------------------------------------

async def run_phase(sc: Scenario, base: str, seconds: float) -> tuple[list[dict], list[dict]]:
    """Send the /ping probe and the workload at the same time."""
    probe = open_loop(base + sc.load.path, sc.load.rate, seconds,
                      keep_alive=sc.load.keep_alive, timeout_s=sc.load.timeout_s)
    if sc.workload is None:
        return await probe, []
    work = open_loop(base + sc.workload.path, sc.workload.rate, seconds,
                     params=sc.workload.params, keep_alive=sc.load.keep_alive,
                     timeout_s=sc.load.timeout_s, read_pid=False)
    pings, works = await asyncio.gather(probe, work)
    return pings, works


async def run_traffic(sc: Scenario, base: str) -> dict:
    if sc.load.warmup_s > 0:
        await run_phase(sc, base, sc.load.warmup_s)   # results thrown away
    start = time.time()
    pings, works = await run_phase(sc, base, sc.load.measure_s)
    return {"start_wall": start, "end_wall": time.time(), "pings": pings, "workload": works}


# ---- analysis --------------------------------------------------------------

def latency_summary(records: list[dict]) -> dict:
    ok = [r["latency_ms"] for r in records if r["status"] == 200]
    errors: dict[str, int] = {}
    for r in records:
        if r["status"] != 200:
            errors[str(r["status"])] = errors.get(str(r["status"]), 0) + 1
    return {
        "count": len(records), "ok": len(ok), "errors": errors,
        "p50_ms": percentile(ok, 50), "p95_ms": percentile(ok, 95),
        "p99_ms": percentile(ok, 99), "max_ms": max(ok, default=float("nan")),
    }


def loop_in_window(run_dir: Path, start: float, end: float) -> dict:
    """Loop samples from the measurement window only. Each worker's samples
    use its own loop clock; clock_ref converts them to wall-clock time."""
    per_pid, all_samples = {}, []
    for f in sorted(run_dir.glob("loop_*.json")):
        data = json.loads(f.read_text())
        ref = data["clock_ref"]
        to_wall = ref["wall_time"] - ref["loop_time"]
        kept = [Sample(**s) for s in data["samples"] if start <= s["actual"] + to_wall <= end]
        per_pid[str(data["pid"])] = summarize(kept)
        all_samples += kept
    return {"all": summarize(all_samples), "per_pid": per_pid}


def parses_in_window(run_dir: Path, start: float, end: float) -> dict:
    path = run_dir / "parses.jsonl"
    if not path.exists():
        return {"count": 0}
    ps = [json.loads(line) for line in path.read_text().splitlines() if line]
    ds = [p["duration_ms"] for p in ps if p["end_wall"] >= start and p["start_wall"] <= end]
    return {"count": len(ds), "p50_ms": percentile(ds, 50), "max_ms": max(ds, default=float("nan"))}


# ---- one run, one scenario -------------------------------------------------

def run_once(sc: Scenario, run_dir: Path, data_dir: Path) -> dict:
    run_dir.mkdir(parents=True)
    config_path = flag_config_for(sc.server.config_flags, data_dir)
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    wall_before = time.monotonic()
    proc = start_server(sc, run_dir, port, config_path)
    try:
        ready = wait_until_ready(proc, base, sc.server.workers, run_dir)
        traffic = asyncio.run(run_traffic(sc, base))
    finally:
        clean = stop_server(proc)
    wall = time.monotonic() - wall_before
    cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_s = (cpu_after.ru_utime - cpu_before.ru_utime) + (cpu_after.ru_stime - cpu_before.ru_stime)

    start, end = traffic["start_wall"], traffic["end_wall"]
    (run_dir / "pings.json").write_text(json.dumps(traffic["pings"]))
    (run_dir / "workload.json").write_text(json.dumps(traffic["workload"]))

    late = [1000 * (p["sent_wall"] - p["planned_wall"]) for p in traffic["pings"]]
    summary = {
        "scenario": sc.name,
        "window_s": end - start,
        "ping": latency_summary(traffic["pings"]),
        # Check the measurer: if pings went out late, the sender itself stalled.
        "probe_send_late_ms": {"p99": percentile(late, 99), "max": max(late, default=0.0)},
        "workload": latency_summary(traffic["workload"]) if sc.workload else None,
        "loop_lag": loop_in_window(run_dir, start, end),
        "parses": parses_in_window(run_dir, start, end),
        # Whole server lifetime (startup + warm-up + measurement + shutdown).
        "server_cpu_s": cpu_s,
        "server_wall_s": wall,
        "server_cpu_pct": 100 * cpu_s / wall if wall else float("nan"),
        "config": {"path": str(config_path), "flags": sc.server.config_flags, "seed": CONFIG_SEED},
        **ready,
        "shutdown_clean": clean,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def run_scenario(sc: Scenario, source: Path, results_dir: Path, data_dir: Path) -> list[dict]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_dir = results_dir / f"{stamp}_{sc.name}"
    base_dir.mkdir(parents=True)
    shutil.copy(source, base_dir / "scenario.toml")                      # exactly what you wrote
    (base_dir / "scenario.json").write_text(json.dumps(asdict(sc), indent=2))  # with defaults filled in
    (base_dir / "environment.json").write_text(json.dumps(environment(), indent=2))

    summaries = []
    for i in range(1, sc.repeat + 1):
        print(f"  {sc.name}: run {i}/{sc.repeat} "
              f"(warm-up {sc.load.warmup_s:g} s, measure {sc.load.measure_s:g} s) ...", flush=True)
        s = run_once(sc, base_dir / f"run_{i}", data_dir)
        s["run"] = i
        summaries.append(s)
        if not s["shutdown_clean"]:
            print(f"  WARNING: server did not stop cleanly; see {base_dir / f'run_{i}' / 'server.log'}")
    print(f"  saved {base_dir}")
    return summaries


# ---- output ----------------------------------------------------------------

def print_table(rows: list[dict]) -> None:
    head = (f"{'scenario':<16} {'run':>3} {'work p50':>9} {'ping p50':>9} {'ping p99':>9} "
            f"{'ping max':>9} {'lag p99':>8} {'lag max':>8} {'CPU%':>5} {'errors':>6} {'late max':>9}")
    print("\n" + head)
    print("-" * len(head))
    for s in rows:
        w = s["workload"]
        errs = sum(s["ping"]["errors"].values()) + (sum(w["errors"].values()) if w else 0)
        lag = s["loop_lag"]["all"]
        work = f"{w['p50_ms']:>9.1f}" if w else f"{'-':>9}"
        print(f"{s['scenario']:<16} {s['run']:>3} {work} "
              f"{s['ping']['p50_ms']:>9.1f} {s['ping']['p99_ms']:>9.1f} {s['ping']['max_ms']:>9.1f} "
              f"{lag['p99_ms']:>8.1f} {lag['max_ms']:>8.1f} {s['server_cpu_pct']:>5.0f} "
              f"{errs:>6} {s['probe_send_late_ms']['max']:>9.1f}")
    print("\nAll times in ms. work = workload request latency, lag = event-loop lag "
          "(all workers),\nCPU% = server CPU over its whole life, late max = how late the "
          "probe sent a ping (checks the measurer).")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenarios", nargs="+", type=Path, help="scenario .toml files")
    ap.add_argument("--results", type=Path, default=ROOT / "results")
    ap.add_argument("--data", type=Path, default=ROOT / "data", help="where flag configs are cached")
    args = ap.parse_args(argv)

    # Check every file first: a typo in the last file should not waste the
    # time spent running the first ones.
    try:
        scenarios = [(load_scenario(p), p) for p in args.scenarios]
    except (ScenarioError, OSError) as e:
        sys.exit(f"error: {e}")

    rows = []
    for sc, path in scenarios:
        print(f"\n{sc.name}: {sc.description}")
        rows += run_scenario(sc, path, args.results, args.data)
    print_table(rows)


if __name__ == "__main__":
    main()
