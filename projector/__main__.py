"""python -m projector seed | artbook [--scenario FILE] [--out FILE]"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LIVE = ("supabase", "pooler")


def local_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url or any(x in url for x in LIVE) or not any(h in url for h in ("127.0.0.1", "localhost", "@db:")):
        raise SystemExit("set DATABASE_URL to a local Postgres; the projector never uses the live database")
    return url


def cmd_seed(args) -> None:
    from kb import consumer, db, reconcile, rules
    from kb.config import load_settings
    from kb.db import connect
    from kb.ingest import FileReader, sync_current
    from kb.loader import load_contract, load_envelopes
    url, s = local_url(), load_settings()
    current_dir = s.data_dir / "current"
    current = {p.relative_to(current_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted(current_dir.rglob("*")) if p.is_file()}
    if not current:
        raise SystemExit(f"no files in {current_dir}")
    with connect(url, "kb") as conn:
        db.migrate(conn, s.schema)
        rules.seed(conn, s.seed_path)
        conn.commit()
        cm = consumer._confidence_map(conn)
        load_contract(conn, s.structured_dir)
        load_envelopes(conn, s.structured_dir, s.data_dir, cm)
        print("files", sync_current(conn, current, FileReader(s.data_dir), s.structured_dir, cm))
        r = reconcile.run(conn, "manual", {"by": "projector seed"})
        print("reconcile", {k: r[k] for k in ("believed", "issues")})


def cmd_artbook(args) -> None:
    from kb.db import connect
    from .artbook import build
    from .world import World
    scenario = yaml.safe_load(Path(args.scenario).read_text())
    with connect(local_url(), "kb") as conn:
        records = build(World.load(conn), scenario)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in records))
    print(f"{len(records)} records written to {out}")


def cmd_control(args) -> None:
    from kb.db import connect
    from .control import Control, serve
    from .world import World
    scenario = yaml.safe_load(Path(args.scenario).read_text())
    with connect(local_url(), "kb") as conn:
        world = World.load(conn)
    control = Control(world, scenario, Path(args.log), Path(args.base), args.screen.rstrip("/"))
    print(f"base beat written to {args.base}; control page on http://127.0.0.1:{args.port}", flush=True)
    serve(control, args.port)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="projector")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed", help="seed kb on a local database from data/current").set_defaults(func=cmd_seed)
    p = sub.add_parser("artbook", help="write the art-book record script from kb and sourcing")
    p.add_argument("--scenario", default=str(ROOT / "projector" / "scenarios" / "artbook.yaml"))
    p.add_argument("--out", default=str(ROOT / "data" / "lookahead" / "artbook.jsonl"))
    p.set_defaults(func=cmd_artbook)
    p = sub.add_parser("control", help="serve the scenario control page")
    p.add_argument("--scenario", default=str(ROOT / "projector" / "scenarios" / "artbook.yaml"))
    p.add_argument("--log", default=str(ROOT / "data" / "lab" / "log.jsonl"))
    p.add_argument("--base", default=str(ROOT / "data" / "lab" / "base.jsonl"))
    p.add_argument("--screen", default="http://127.0.0.1:8791")
    p.add_argument("--port", type=int, default=8792)
    p.set_defaults(func=cmd_control)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
