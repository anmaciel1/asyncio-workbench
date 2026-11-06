"""Generate the fake feature-flag config used by the json_parse workload.

The file is large and fully reproducible from (flags, seed), so it is not
committed. Regenerate it with:

  uv run python scripts/make_flag_config.py              # default size
  uv run python scripts/make_flag_config.py --flags 5000 # smaller config
"""

import argparse
import asyncio
import time
from pathlib import Path

from workbench.flag_config import parse_config, read_config_bytes, write_config

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "flag_config.json"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--flags", type=int, default=6_000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, default=DEFAULT_PATH)
    args = ap.parse_args()

    path = write_config(args.out, args.flags, args.seed)
    raw = read_config_bytes(path)

    # Time a few parses so you know how long one stall will be on this machine.
    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        rules = asyncio.run(parse_config(raw))
        times.append(time.perf_counter() - t0)
    best_ms = min(times) * 1000

    print(f"wrote {path}")
    print(f"  flags={args.flags}  seed={args.seed}  rules={rules}  "
          f"size={len(raw) / 1_000_000:.1f} MB")
    print(f"  one parse takes ~{best_ms:.0f} ms on this machine (best of 5)")


if __name__ == "__main__":
    main()
