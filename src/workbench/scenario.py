"""Scenario files: one TOML file describes one experiment.

    name = "cpu-work"
    description = "..."
    repeat = 1                      # how many times to run it

    [server]                        # how the server is started
    workers = 1
    poll_interval = 0               # 0 = background config poller off

    [load]                          # the victim: /ping at a fixed rate
    rate = 50
    warmup_s = 3                    # sent, then thrown away
    measure_s = 20                  # kept

    [workload]                      # optional: the work that competes with /ping
    path = "/work/cpu"
    params = { n = 95000 }
    rate = 4                        # requests per second

``tomllib`` only checks the syntax. The Pydantic models below check the
contents, the same way FastAPI checks request parameters:

* ``extra="forbid"``: an unknown key (a typo like ``workres``) is an error,
  instead of being ignored while the default is silently used.
* ``strict=True``: no type guessing. ``"8"`` or ``true`` is not a number.
* ``Field(ge=...)``: the smallest allowed value.
* ``frozen=True``: a loaded scenario cannot be changed by accident.
"""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ScenarioError(ValueError):
    """The scenario file is not valid. The message says what to fix."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ServerConfig(_Strict):
    workers: int = Field(default=1, ge=1)
    # Only the standard loop is installed. Keeping it explicit means a loop
    # change can never happen silently between runs.
    loop: Literal["asyncio"] = "asyncio"
    loop_interval: float = Field(default=0.01, ge=0.001)
    poll_interval: float = Field(default=0.0, ge=0)   # 0 = poller off
    poll_jitter: float = Field(default=0.0, ge=0)
    config_flags: int = Field(default=6_000, ge=1)    # size of the generated flag config


class LoadConfig(_Strict):
    path: str = Field(default="/ping", pattern=r"^/")
    rate: float = Field(default=50, ge=0.1)
    warmup_s: float = Field(default=3, ge=0)
    measure_s: float = Field(default=20, ge=1)
    timeout_s: float = Field(default=10, ge=0.1)
    keep_alive: bool = False


class WorkloadConfig(_Strict):
    path: str = Field(pattern=r"^/")
    rate: float = Field(ge=0.1)
    params: dict[str, str | int | float | bool] = {}


class Scenario(_Strict):
    name: str = Field(min_length=1)
    description: str = ""
    repeat: int = Field(default=1, ge=1)
    server: ServerConfig = ServerConfig()
    load: LoadConfig = LoadConfig()
    workload: WorkloadConfig | None = None


def _explain(e: ValidationError) -> str:
    """One line per problem, e.g. 'server.workres: Extra inputs are not permitted'."""
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc']) or '(file)'}: {err['msg']}"
        for err in e.errors()
    )


def parse_scenario(data: dict) -> Scenario:
    try:
        return Scenario.model_validate(data)
    except ValidationError as e:
        raise ScenarioError(_explain(e)) from None


def load_scenario(path: Path) -> Scenario:
    try:
        with open(path, "rb") as f:   # tomllib reads bytes
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ScenarioError(f"{path}: not valid TOML: {e}") from None
    try:
        return parse_scenario(data)
    except ScenarioError as e:
        raise ScenarioError(f"{path}: {e}") from None
