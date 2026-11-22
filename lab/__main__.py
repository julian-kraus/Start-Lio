"""CLI: python -m lab <command>.

  init                     start or reuse a local Postgres and create the kb, mail, plan and lab schemas
  seed --from data/current ingest every file of the snapshot through kb.ingest and reconcile (no tracker)
  snapshot <name>          pg_dump the whole lab database to data/lab/<name>.dump
  restore  <name>          restore the lab database from data/lab/<name>.dump
  status                   counts of files, facts, believed values, issues, mail, Lookahead records, the clock
"""

from __future__ import annotations

import argparse
import json
import sys

from . import db, seed as seed_mod


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _url(managed_ok: bool) -> tuple[str, bool]:
    url, managed = db.resolve_url()
    if managed and managed_ok:
        url = db.ensure_managed_db()
    db.assert_local(url)
    return url, managed


def cmd_init(args) -> None:
    url, managed = _url(managed_ok=True)
    applied = db.create_schemas(url, db.schemas())
    _print({"database": "managed lab-db" if managed else db.db_label(url), "applied": applied})


def cmd_seed(args) -> None:
    url, _ = _url(managed_ok=True)
    root = seed_mod.data_root(args.src)
    _print(seed_mod.seed(url, db.schemas(), root))


def cmd_snapshot(args) -> None:
    url, managed = _url(managed_ok=True)
    path = db.snapshot(url, args.name, db.schemas(), managed)
    _print({"snapshot": str(path)})


def cmd_restore(args) -> None:
    url, managed = _url(managed_ok=True)
    path = db.restore(url, args.name, db.schemas(), managed)
    _print({"restored": str(path)})


def cmd_status(args) -> None:
    url, _ = _url(managed_ok=False)
    _print(seed_mod.status(url, db.schemas()))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create or update the kb, mail, plan and lab schemas on a local Postgres").set_defaults(
        func=cmd_init)
    p = sub.add_parser("seed", help="ingest a portal snapshot through kb and reconcile, without the tracker")
    p.add_argument("--from", dest="src", default="data/current", help="data dir with current/, or a current/ dir")
    p.set_defaults(func=cmd_seed)
    p = sub.add_parser("snapshot", help="save the whole lab database to data/lab/<name>.dump")
    p.add_argument("name")
    p.set_defaults(func=cmd_snapshot)
    p = sub.add_parser("restore", help="restore the lab database from data/lab/<name>.dump")
    p.add_argument("name")
    p.set_defaults(func=cmd_restore)
    sub.add_parser("status", help="counts and the scenario clock").set_defaults(func=cmd_status)
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
