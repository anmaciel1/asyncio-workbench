import asyncio
import json
import os
import platform
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from typing import Literal

from fastapi import FastAPI, Query, Request, Response

from workbench.config_poller import poll_config
from workbench.flag_config import parse_config, read_config_bytes, write_config
from workbench.loop_monitor import LoopMonitor
from workbench.payload import PayloadCache, records_for_kb
from workbench.stats import summarize
from workbench.workloads import async_wait, blocking_wait, cpu_work

ROOT = Path(__file__).resolve().parents[2]

# Upper bounds keep every workload input finite: one bad request cannot ask
# for an hour of sleep or a billion-number prime count.
MAX_MS = 5_000
MAX_N = 2_000_000
MAX_KB = 50_000  # 50 MB of JSON


@dataclass(frozen=True)
class Settings:
    run_dir: Path | None = None
    config_path: Path = ROOT / "data" / "flag_config.json"
    loop_interval: float = 0.01
    poll_interval: float | None = None  # None = no background config poller
    poll_jitter: float = 0.0            # random 0..jitter s added to each poll

    @classmethod
    def from_env(cls) -> "Settings":
        run_dir = os.environ.get("WORKBENCH_RUN_DIR")
        poll_interval = os.environ.get("WORKBENCH_POLL_INTERVAL")
        return cls(
            run_dir=Path(run_dir) if run_dir else None,
            config_path=Path(os.environ.get("WORKBENCH_CONFIG", cls.config_path)),
            loop_interval=float(os.environ.get("WORKBENCH_LOOP_INTERVAL", cls.loop_interval)),
            poll_interval=float(poll_interval) if poll_interval else None,
            poll_jitter=float(os.environ.get("WORKBENCH_POLL_JITTER", cls.poll_jitter)),
        )


def which_process_and_thread() -> dict:
    """Which process and thread is running this code right now."""
    return {"pid": os.getpid(), "thread": threading.current_thread().name}


async def cancel_and_wait(task: asyncio.Task) -> None:
    """Cancel a background task and wait until it has really finished."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        # Same rule as LoopMonitor.stop(): only swallow the task's own
        # cancellation, never a cancellation aimed at the caller.
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # ---- startup: runs once, before the first request ----------------
        if not settings.config_path.exists():
            write_config(settings.config_path, num_flags=6_000, seed=0)
        app.state.config_raw = read_config_bytes(settings.config_path)

        # Two clocks at the same instant: loop.time() is monotonic but only
        # meaningful inside this process; time.time() is wall clock and can
        # be compared with the load generator's timestamps.
        app.state.clock_ref = {
            "loop_time": asyncio.get_running_loop().time(),
            "wall_time": time.time(),
        }
        monitor = LoopMonitor(interval=settings.loop_interval)
        app.state.monitor = monitor

        # The OpenAI-style background job: parse the config every N seconds.
        app.state.parses = []
        app.state.payloads = PayloadCache()
        poller = None
        if settings.poll_interval:
            log_path = None
            if settings.run_dir is not None:
                settings.run_dir.mkdir(parents=True, exist_ok=True)
                log_path = settings.run_dir / "parses.jsonl"
            poller = asyncio.create_task(
                poll_config(settings.config_path, settings.poll_interval,
                            settings.poll_jitter, app.state.parses, log_path),
                name="config-poller",
            )

        try:
            async with monitor:
                yield  # ---- the server handles requests while we are paused here
        finally:
            if poller is not None:
                await cancel_and_wait(poller)
        # ---- shutdown: the monitor has been stopped by `async with` -----
        if settings.run_dir is not None:
            save_loop_samples(settings, app.state.clock_ref, monitor)

    app = FastAPI(title="Asyncio Performance Workbench", lifespan=lifespan)

    @app.get("/ping")
    async def ping():
        return {"ok": True, **which_process_and_thread()}

    @app.get("/work/async_wait")
    async def work_async_wait(ms: float = Query(100, ge=0, le=MAX_MS)):
        await async_wait(ms / 1000)
        return {"workload": "async_wait", "ms": ms, **which_process_and_thread()}

    @app.get("/work/blocking_wait")
    async def work_blocking_wait(ms: float = Query(100, ge=0, le=MAX_MS)):
        await blocking_wait(ms / 1000)
        return {"workload": "blocking_wait", "ms": ms, **which_process_and_thread()}

    @app.get("/work/blocking_wait_def")
    def work_blocking_wait_def(ms: float = Query(100, ge=0, le=MAX_MS)):
        # Plain `def`: FastAPI runs this in a worker thread, NOT on the loop.
        time.sleep(ms / 1000)
        return {"workload": "blocking_wait_def", "ms": ms, **which_process_and_thread()}

    @app.get("/work/cpu")
    async def work_cpu(n: int = Query(60_000, ge=0, le=MAX_N)):
        primes = await cpu_work(n)
        return {"workload": "cpu", "n": n, "primes": primes, **which_process_and_thread()}

    @app.get("/work/json_parse")
    async def work_json_parse(request: Request):
        rules = await parse_config(request.app.state.config_raw)
        return {"workload": "json_parse", "rules": rules, **which_process_and_thread()}

    @app.get("/work/payload")
    async def work_payload(
        request: Request,
        kb: int = Query(1_000, ge=1, le=MAX_KB),
        mode: Literal["dict", "dumps", "bytes"] = "dict",
    ):
        # Same data three ways; see payload.py. Call each size once first:
        # the first call builds (and for `bytes`, converts) and caches it.
        cache: PayloadCache = request.app.state.payloads
        n = records_for_kb(kb)
        if mode == "dict":
            # FastAPI converts this dict to JSON after we return, on the loop.
            return {"records": cache.get_records(n)}
        if mode == "dumps":
            # We convert it ourselves, still on the loop, skipping FastAPI's walk.
            body = json.dumps({"records": cache.get_records(n)}).encode()
            return Response(content=body, media_type="application/json")
        # Converted earlier and cached: only sending is left.
        return Response(content=cache.get_bytes(n), media_type="application/json")

    @app.get("/loop")
    async def loop_summary(request: Request):
        monitor: LoopMonitor = request.app.state.monitor
        return {"interval_s": monitor.interval, "summary": summarize(monitor.samples), **which_process_and_thread()}

    @app.get("/parses")
    async def recent_parses(request: Request):
        """The last few background parses done by THIS process."""
        return {"parses": request.app.state.parses[-10:], **which_process_and_thread()}

    return app


def save_loop_samples(settings: Settings, clock_ref: dict, monitor: LoopMonitor) -> Path:
    settings.run_dir.mkdir(parents=True, exist_ok=True)
    path = settings.run_dir / f"loop_{monitor.pid}.json"
    path.write_text(json.dumps({
        "pid": monitor.pid,
        "interval_s": monitor.interval,
        "clock_ref": clock_ref,
        "environment": {
            "python": sys.version,
            "gil_enabled": getattr(sys, "_is_gil_enabled", lambda: True)(),
            "platform": platform.platform(),
        },
        "summary": summarize(monitor.samples),
        "samples": [asdict(s) for s in monitor.samples],
    }, indent=2))
    return path
