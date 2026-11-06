import asyncio
import json

from workbench.flag_config import generate_config, parse_config, read_config_bytes, write_config


def test_generate_config_is_deterministic():
    assert generate_config(50, seed=1) == generate_config(50, seed=1)
    assert generate_config(50, seed=1) != generate_config(50, seed=2)


def test_parse_config_counts_rules_from_file(tmp_path):
    path = write_config(tmp_path / "flags.json", num_flags=200, seed=3)
    expected = sum(len(f["rules"]) for f in generate_config(200, seed=3)["flags"])
    raw = read_config_bytes(path)
    assert asyncio.run(parse_config(raw)) == expected
    assert json.loads(raw)["version"] == 3
