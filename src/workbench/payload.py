"""Large response payloads: how much does it cost to SEND data vs. CONVERT it?

The payload is a list of small, user-like records (~180 bytes of JSON each),
the kind of thing a "list" endpoint returns. Records are built once and
cached, so a request measures only the cost of returning them, not of
building them.

Three ways to return the same data (see app.py, /work/payload):

* ``dict``  - return a Python dict and let FastAPI convert it. FastAPI first
              walks every object (``jsonable_encoder``, pure Python), then
              calls ``json.dumps``. Both run ON THE LOOP.
* ``dumps`` - call ``json.dumps`` ourselves and return the bytes. Skips
              FastAPI's walk, but ``json.dumps`` still runs on the loop.
* ``bytes`` - return bytes that were converted earlier and cached. Nothing to
              convert, only sending, which is I/O and frees the loop.
"""

import json
from dataclasses import dataclass, field

TAGS = ["admin", "beta", "billing", "eu", "us", "mobile", "trial", "vip"]


def make_record(i: int) -> dict:
    """One deterministic record. Same i -> same record."""
    return {
        "id": i,
        "name": f"user-{i:07d}",
        "email": f"user{i}@example.com",
        "active": i % 3 != 0,
        "score": round((i * 7919 % 10_000) / 100, 2),
        "tags": [TAGS[i % 8], TAGS[(i * 3) % 8]],
        "created": f"2026-{i % 12 + 1:02d}-{i % 28 + 1:02d}T12:00:00Z",
    }


# Average size of one record as JSON, measured once from a sample.
RECORD_BYTES = len(json.dumps([make_record(i) for i in range(1000)])) / 1000


def records_for_kb(kb: int) -> int:
    """How many records make roughly ``kb`` kilobytes of JSON."""
    return max(1, round(kb * 1000 / RECORD_BYTES))


@dataclass
class PayloadCache:
    """Builds records once and keeps them, plus pre-converted bytes per size."""

    records: list[dict] = field(default_factory=list)
    encoded: dict[int, bytes] = field(default_factory=dict)

    def get_records(self, n: int) -> list[dict]:
        # Grow the cache only when a bigger size is asked for. Slicing a list
        # copies pointers, not records, so it is cheap.
        if n > len(self.records):
            self.records.extend(make_record(i) for i in range(len(self.records), n))
        return self.records[:n]

    def get_bytes(self, n: int) -> bytes:
        if n not in self.encoded:
            self.encoded[n] = json.dumps({"records": self.get_records(n)}).encode()
        return self.encoded[n]
