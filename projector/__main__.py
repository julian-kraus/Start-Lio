"""CLI: python -m projector run [--once | --follow].

Projects the Lookahead screen from kb and the sourcing models. `--once` (the default) projects the current
state and exits; `--follow` re-projects on every kb change until stopped.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import launch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="projector")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="project the Lookahead screen from kb and the sourcing models")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="project the current state once and exit (default)")
    mode.add_argument("--follow", action="store_true", help="re-project on every kb change until stopped")
    run.add_argument("--log", default=os.environ.get("LOOKAHEAD_LOG", "lookahead.log"),
                     help="the Lookahead record log to append to")
    run.add_argument("--url", default=os.environ.get("DATABASE_URL") or os.environ.get("TEST_DATABASE_URL"),
                     help="the kb database URL (defaults to DATABASE_URL)")
    run.add_argument("--interval", type=float, default=2.0, help="seconds between re-projections with --follow")
    args = parser.parse_args(argv)

    if not args.url:
        print("set DATABASE_URL (or pass --url)", file=sys.stderr)
        return 2
    fresh = launch.run(Path(args.log), args.url, once=not args.follow, interval=args.interval)
    if not args.follow:
        print(f"projected {len(fresh)} new record(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
