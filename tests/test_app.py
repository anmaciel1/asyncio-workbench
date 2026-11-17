import json

import pytest
from fastapi.testclient import TestClient

from workbench.app import Settings, create_app
from workbench.flag_config import write_config


@pytest.fixture
def settings(tmp_path):
    config = write_config(tmp_path / "flags.json", num_flags=50, seed=0)
    return Settings(run_dir=tmp_path / "run", config_path=config, loop_interval=0.005)


@pytest.fixture
def client(settings):
    # `with` runs the lifespan: startup before the block, shutdown after.
    with TestClient(create_app(settings)) as c:
        yield c


def test_ping_reports_process_and_thread(client):
    body = client.get("/ping").json()
    assert body["ok"] is True
    assert isinstance(body["pid"], int)


def test_inputs_are_bounded(client):
    assert client.get("/work/async_wait", params={"ms": 10_000_000}).status_code == 422
    assert client.get("/work/cpu", params={"n": -1}).status_code == 422


def test_async_def_runs_on_loop_thread_but_def_runs_in_threadpool(client):
    loop_thread = client.get("/ping").json()["thread"]
    on_loop = client.get("/work/blocking_wait", params={"ms": 1}).json()["thread"]
    in_pool = client.get("/work/blocking_wait_def", params={"ms": 1}).json()["thread"]
    assert on_loop == loop_thread
    assert in_pool != loop_thread


def test_workloads_return_correct_answers(client):
    assert client.get("/work/cpu", params={"n": 10_000}).json()["primes"] == 1229
    assert client.get("/work/json_parse").json()["rules"] > 0


def test_shutdown_saves_loop_samples(settings):
    with TestClient(create_app(settings)) as c:
        c.get("/work/blocking_wait", params={"ms": 50})
    files = list(settings.run_dir.glob("loop_*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["summary"]["count"] > 0
    assert data["summary"]["max_ms"] >= 40  # the 50 ms block shows up as lag
    assert {"loop_time", "wall_time"} <= data["clock_ref"].keys()
