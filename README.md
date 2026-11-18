# Asyncio Performance Workbench
<p align="center">
  <img width="1536" height="1024" alt="frog-asyncio" src="https://github.com/user-attachments/assets/0e2bbff0-5431-4d3f-a769-47de1fa2e426" />
</p>

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

## Milestone 2: the OpenAI incident in miniature

Each server process runs a background poller that parses the feature-flag
config every N seconds on the event loop. `scripts/ping_probe.py` sends
`/ping` at a fixed rate and lines the latencies up with the parses.

```bash
# terminal 1: 8 processes, all parsing together every 5 s (no jitter)
WORKBENCH_RUN_DIR=results/sync WORKBENCH_POLL_INTERVAL=5 \
  uv run uvicorn workbench.app:create_app --factory --loop asyncio --workers 8

# terminal 2
uv run python scripts/ping_probe.py --run-dir results/sync

# then repeat with jitter (spread each process's parse over 0..5 s)
WORKBENCH_RUN_DIR=results/jitter WORKBENCH_POLL_INTERVAL=5 WORKBENCH_POLL_JITTER=5 \
  uv run uvicorn workbench.app:create_app --factory --loop asyncio --workers 8
uv run python scripts/ping_probe.py --run-dir results/jitter
```

| Setting | Meaning |
|---|---|
| `WORKBENCH_POLL_INTERVAL` | seconds between config parses (unset = poller off) |
| `WORKBENCH_POLL_JITTER` | random 0..N seconds added to each poll, per process |
| `WORKBENCH_RUN_DIR` | where `parses.jsonl`, `loop_<pid>.json` and `pings.json` go |

### Large responses: sending vs. converting

`/work/payload?kb=&mode=` returns the same records three ways:
`dict` (FastAPI converts), `dumps` (we call `json.dumps`), `bytes`
(converted earlier and cached, so only sending is left). Call each size once
first: the first call builds and caches the data.

```bash
F='%{time_total}s\n'
curl -s -o /dev/null 'localhost:8000/work/payload?kb=5000&mode=dict'   # warm-up
curl -s -o /dev/null 'localhost:8000/work/payload?kb=5000&mode=dict' & sleep 0.02; \
  curl -s -o /dev/null -w "ping during payload: $F" localhost:8000/ping
```

## Milestone 3: repeatable runner

One TOML file in `scenarios/` describes one experiment. The runner starts the
server, waits until it answers, runs a warm-up (thrown away), measures, stops
the server cleanly and saves everything to `results/<time>_<name>/`.

```bash
uv run python -m workbench.runner scenarios/cpu_work.toml   # one scenario
uv run python -m workbench.runner scenarios/*.toml          # all, then a comparison table
```

| Scenario | Workload (4 requests/s, ~100 ms each) next to `/ping` at 50/s |
|---|---|
| `baseline` | none |
| `async_wait` | `await asyncio.sleep(0.1)` |
| `blocking_wait` | `time.sleep(0.1)` on the loop |
| `cpu_work` | count primes, `n=95000` |
| `json_parse` | `json.loads` of the 6000-flag config |
| `data_transfer` | ~1.6 MB response returned as a dict |
| `jitter` | 8 workers, config parse every 5 s with jitter |

Each results folder holds `scenario.toml`, `scenario.json` (with defaults
filled in), `environment.json` (Python, packages, git commit), and per run:
`pings.json`, `workload.json`, `loop_<pid>.json`, `server.log`, `summary.json`.

## Layout

```
src/workbench/workloads.py     async wait, blocking wait, CPU work
src/workbench/loop_monitor.py  heartbeat lag recorder
src/workbench/stats.py         percentiles and summaries
src/workbench/flag_config.py   fake feature-flag config: generate, read, parse
scripts/heartbeat_demo.py      Milestone 1 demo
tests/                         workload correctness, lag math, monitor behavior
```

