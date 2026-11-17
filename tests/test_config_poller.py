import json
import random
import time

from fastapi.testclient import TestClient

from workbench.app import Settings, create_app
from workbench.config_poller import next_delay
from workbench.flag_config import write_config


def test_without_jitter_every_process_wakes_on_the_same_boundary():
    rng = random.Random(0)
    # Two "processes" asking at different moments wake at the same tick: t=10.
    for now in (5.1, 7.9, 9.99):
        assert abs((now + next_delay(now, 5, 0, rng)) - 10.0) < 1e-9


def test_jitter_stays_within_bounds():
    rng = random.Random(0)
    for _ in range(100):
        delay = next_delay(3.0, 5, 2, rng)
        assert 2.0 <= delay <= 4.0  # 2 s to the tick, plus 0..2 s jitter


def test_poller_parses_in_background_and_logs(tmp_path):
    config = write_config(tmp_path / "flags.json", num_flags=50, seed=0)
    settings = Settings(run_dir=tmp_path / "run", config_path=config,
                        loop_interval=0.005, poll_interval=0.1)
    with TestClient(create_app(settings)) as c:
        time.sleep(0.35)
        assert len(c.get("/parses").json()["parses"]) >= 2
    lines = (settings.run_dir / "parses.jsonl").read_text().splitlines()
    event = json.loads(lines[0])
    assert event["rules"] > 0
    assert event["end_wall"] >= event["start_wall"]
