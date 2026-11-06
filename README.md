# Asyncio Performance Workbench

A local tool for explaining **why an asynchronous Python service becomes slow**.
It runs controlled workloads, measures event-loop scheduling delay (lag), and
compares changes to concurrency, worker processes, and where expensive work runs.

> Main question: when a service slows down, where is work waiting, and which
> change improves responsiveness under the same resource budget?

## Setup

Requires [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 automatically.

```bash
uv sync
uv run pytest
```

## Milestone 1: scheduling demonstration

A heartbeat runs every 10 ms next to each workload in turn. The script reports
how late it woke up:

| Phase           | What runs on the loop                    |
| --------------- | ---------------------------------------- |
| `idle`          | nothing (baseline)                       |
| `async_wait`    | `await asyncio.sleep(0.1)`               |
| `blocking_wait` | `time.sleep(0.1)` inside a coroutine     |
| `cpu_work`      | fixed-size pure-Python prime count (~0.1 s) |
| `json_parse`    | `json.loads` of a fake 8 MB feature-flag config (~0.08 s), modelled on [OpenAI's Habitat/Statsig stall](https://openai.com/index/scaling-storage-one-billion-users-part-one/) |

```bash
uv run python scripts/make_flag_config.py   # generates data/flag_config.json (gitignored)
uv run python scripts/heartbeat_demo.py
# freeze the CPU workload size after the first calibration:
uv run python scripts/heartbeat_demo.py --cpu-n <value printed on first run>
```

Raw samples and the run configuration are saved to `results/<run_id>/heartbeat.json`.

## Layout

```
src/workbench/workloads.py     async wait, blocking wait, CPU work
src/workbench/loop_monitor.py  heartbeat lag recorder
src/workbench/stats.py         percentiles and summaries
src/workbench/flag_config.py   fake feature-flag config: generate, read, parse
scripts/heartbeat_demo.py      Milestone 1 demo
tests/                         workload correctness, lag math, monitor behavior
```

## Roadmap

1. Scheduling demonstration ✅
2. Instrumented API (FastAPI `/ping` + workload route + loop monitor in lifespan)
3. Repeatable runner (scenario config, warm-up, artifacts)
4. First comparison report
5. Controlled improvements (offloading, admission limits, worker counts)
6. Portfolio release
