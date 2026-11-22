"""python -m plan run: the monitor loop of the planning layer."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def cmd_run(args) -> int:
    from agents import llm, monitor
    from plan import db

    if not args.database_url:
        print("set DATABASE_URL or pass --database-url", file=sys.stderr)
        return 2
    if args.no_model:
        client = llm.ReplayClient([], model="none")    # every role falls back to the recorded deterministic result
    else:
        try:
            client = llm.AnthropicClient()
        except RuntimeError as exc:
            print(f"{exc}. --no-model runs on the deterministic results alone", file=sys.stderr)
            return 2
    with db.connect(args.database_url) as conn:
        db.migrate(conn)
        conn.commit()
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    monitor.run(args.database_url, client, args.log, stop, kb_schema=os.environ.get("KB_SCHEMA", "kb"))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m plan")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="watch promises, re-plan when one breaks and publish to the Lookahead log")
    p.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    p.add_argument("--log", type=Path,
                   default=Path(os.environ.get("LOOKAHEAD_LOG") or ROOT / "data" / "lookahead" / "log.jsonl"))
    p.add_argument("--no-model", action="store_true", help="skip the debate, keep the deterministic verdict")
    p.set_defaults(func=cmd_run)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
