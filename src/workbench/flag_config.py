"""A fake feature-flag config, modelled on the OpenAI Habitat incident.

In the article, every process polled Statsig once a minute and parsed a JSON
config holding *every production rule across every service*. Parsing that
file ran on the event loop and stalled in-flight requests.

This module:
  * ``generate_config``: builds a deterministic config of that shape. The
    same ``num_flags`` and ``seed`` always give the same bytes.
  * ``write_config`` / ``read_config_bytes``: save it to a file and load it back.
  * ``parse_config``: the stand-in for that parsing step. It runs
    ``json.loads`` on the loop's thread.

After creating stand in functions, I decided to just recreate the problem itself, on a
very small scale with this file
"""

import json
import random
from pathlib import Path

SERVICES = [
    "chat", "api", "codex", "auth", "billing", "search", "storage",
    "images", "voice", "admin", "ads", "memory",
]
OPERATORS = ["any", "none", "gt", "lt", "eq", "str_contains_any", "version_gte"]
FIELDS = ["user_id", "country", "app_version", "plan", "org_id", "locale", "platform"]


def generate_config(num_flags: int, seed: int = 0) -> dict:
    """Build a config with ``num_flags`` flags, each with a few targeting rules."""
    rng = random.Random(seed)  
    flags = []
    for i in range(num_flags):
        service = SERVICES[i % len(SERVICES)]
        rules = []
        for r in range(rng.randint(2, 6)):
            rules.append({
                "id": f"rule_{i}_{r}",
                "pass_percentage": rng.choice([0, 1, 5, 10, 25, 50, 100]),
                "conditions": [
                    {
                        "field": rng.choice(FIELDS),
                        "operator": rng.choice(OPERATORS),
                        "target_value": [
                            f"v{rng.randint(0, 99_999)}" for _ in range(rng.randint(1, 8))
                        ],
                    }
                    for _ in range(rng.randint(1, 3))
                ],
                "return_value": {"enabled": rng.random() < 0.5, "variant": rng.randint(0, 3)},
            })
        flags.append({
            "name": f"{service}.flag_{i}",
            "service": service,
            "enabled": rng.random() < 0.9,
            "salt": f"{rng.getrandbits(64):016x}",
            "rules": rules,
        })
    return {"version": seed, "flags": flags}


def write_config(path: Path, num_flags: int, seed: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(generate_config(num_flags, seed)))
    return path


def read_config_bytes(path: Path) -> bytes:
    """Read the raw file. Plays the role of *fetching* the config."""
    return Path(path).read_bytes()


async def parse_config(raw: bytes) -> int:
    """Parse the config ON THE EVENT LOOP and return the number of rules.

    Like ``cpu_work``, this is ``async def`` with no ``await`` inside.
    ``json.loads`` holds the loop's thread until it finishes. That matches
    the article: a background refresh coroutine parsing in place.

    The rule count is returned so callers and tests can check that the
    parse really happened.
    """
    config = json.loads(raw)
    return sum(len(flag["rules"]) for flag in config["flags"])
