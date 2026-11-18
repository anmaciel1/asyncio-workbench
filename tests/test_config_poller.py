import json
import random
import time

from fastapi.testclient import TestClient

from workbench.app import Settings, create_app
from workbench.config_poller import next_delay
from workbench.flag_config import write_config


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
