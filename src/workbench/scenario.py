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

``tomllib`` only checks the syntax. ``load_scenario`` also checks the
contents (unknown keys, wrong types, out-of-range values), so a typo fails
before a run starts instead of after it.
"""

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


class ScenarioError(ValueError):
    """The scenario file is not valid. The message says what to fix."""


def _number(where: str, name: str, value, *, minimum: float = 0.0, integer: bool = False):
    # bool is a subclass of int in Python, so reject it explicitly.
    ok_types = (int,) if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, ok_types):
        kind = "a whole number" if integer else "a number"
        raise ScenarioError(f"{where}.{name} must be {kind}, got {value!r}")
    if value < minimum:
        raise ScenarioError(f"{where}.{name} must be >= {minimum}, got {value!r}")
    return value


@dataclass(frozen=True)
class ServerConfig:
    workers: int = 1
    loop: str = "asyncio"
    loop_interval: float = 0.01
    poll_interval: float = 0.0   # 0 = poller off
    poll_jitter: float = 0.0
    config_flags: int = 6_000    # size of the generated flag config

    def check(self, where: str) -> None:
        _number(where, "workers", self.workers, minimum=1, integer=True)
        _number(where, "loop_interval", self.loop_interval, minimum=0.001)
        _number(where, "poll_interval", self.poll_interval)
        _number(where, "poll_jitter", self.poll_jitter)
        _number(where, "config_flags", self.config_flags, minimum=1, integer=True)
        if self.loop != "asyncio":
            # Only the standard loop is installed. Keeping it explicit means a
            # loop change can never happen silently between runs.
            raise ScenarioError(f"{where}.loop must be 'asyncio', got {self.loop!r}")


@dataclass(frozen=True)
class LoadConfig:
    path: str = "/ping"
    rate: float = 50
    warmup_s: float = 3
    measure_s: float = 20
    timeout_s: float = 10
    keep_alive: bool = False

    def check(self, where: str) -> None:
        _number(where, "rate", self.rate, minimum=0.1)
        _number(where, "warmup_s", self.warmup_s)
        _number(where, "measure_s", self.measure_s, minimum=1)
        _number(where, "timeout_s", self.timeout_s, minimum=0.1)
        if not isinstance(self.keep_alive, bool):
            raise ScenarioError(f"{where}.keep_alive must be true or false")
        if not self.path.startswith("/"):
            raise ScenarioError(f"{where}.path must start with '/', got {self.path!r}")


@dataclass(frozen=True)
class WorkloadConfig:
    path: str
    rate: float
    params: dict = field(default_factory=dict)

    def check(self, where: str) -> None:
        _number(where, "rate", self.rate, minimum=0.1)
        if not self.path.startswith("/"):
            raise ScenarioError(f"{where}.path must start with '/', got {self.path!r}")
        if not isinstance(self.params, dict):
            raise ScenarioError(f"{where}.params must be a table, e.g. {{ ms = 100 }}")


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str = ""
    repeat: int = 1
    server: ServerConfig = field(default_factory=ServerConfig)
    load: LoadConfig = field(default_factory=LoadConfig)
    workload: WorkloadConfig | None = None


def _build(cls, data, where: str):
    if not isinstance(data, dict):
        raise ScenarioError(f"[{where}] must be a table")
    allowed = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ScenarioError(
            f"[{where}] has unknown setting(s) {unknown}. Allowed: {sorted(allowed)}")
    try:
        obj = cls(**data)
    except TypeError as e:  # e.g. a required setting is missing
        raise ScenarioError(f"[{where}] {e}") from None
    obj.check(where)
    return obj


def parse_scenario(data: dict) -> Scenario:
    top = {k: v for k, v in data.items() if k not in ("server", "load", "workload")}
    allowed = {"name", "description", "repeat"}
    unknown = sorted(set(top) - allowed)
    if unknown:
        raise ScenarioError(f"unknown top-level setting(s) {unknown}. Allowed: {sorted(allowed)}")
    if not isinstance(top.get("name"), str) or not top["name"]:
        raise ScenarioError("'name' is required and must be text")
    repeat = _number("top", "repeat", top.get("repeat", 1), minimum=1, integer=True)
    return Scenario(
        name=top["name"],
        description=top.get("description", ""),
        repeat=int(repeat),
        server=_build(ServerConfig, data.get("server", {}), "server"),
        load=_build(LoadConfig, data.get("load", {}), "load"),
        workload=_build(WorkloadConfig, data["workload"], "workload") if "workload" in data else None,
    )


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
