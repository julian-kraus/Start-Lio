"""Seed the lab from a portal snapshot and report its state.

Seeding reuses kb's own ingest and reconcile, driven from the snapshot's files instead of the portal tracker:
`kb.consumer.sync` reads every current file (no `public` change feed, no portal client), then `kb.reconcile`
folds the facts into believed values and issues. It is idempotent: a file kb has already processed is skipped,
and reconcile is deterministic, so a second seed adds nothing."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from tracker.config import PROJECT_ROOT

from . import db
from .db import Schemas


def data_root(src: str | Path) -> Path:
    """Resolve `--from` to a kb data directory that contains a `current/` snapshot.

    Accepts the data directory itself (it has `current/`) or the `current/` snapshot directory directly, so both
    `--from data` and `--from data/current` work."""
    path = Path(src).resolve()
    if (path / "current").is_dir():
        return path
    if path.name == "current" and path.is_dir():
        return path.parent
    raise SystemExit(f"--from {src!r} is not a data dir with current/, nor a current/ snapshot dir")


def _kb_settings(url: str, s: Schemas, root: Path):
    from kb import __file__ as kb_init
    from kb.config import KbSettings
    return KbSettings(
        database_url=url,
        schema=s.kb,
        data_dir=root,
        structured_dir=PROJECT_ROOT / "structured",
        seed_path=Path(kb_init).resolve().parent / "rules.seed.yaml",
        llm_adapter=None,
    )


def _snapshot_shas(current: Path) -> dict[str, str]:
    """Hash every file in the snapshot's current/ directory. The lab seeds from the files on disk, never from the
    tracker's `public.file_versions`, so it holds even on a database the portal tracker once wrote to."""
    out: dict[str, str] = {}
    for path in current.rglob("*"):
        if path.is_file():
            out[str(path.relative_to(current))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def seed(url: str, s: Schemas, root: Path) -> dict:
    """Ingest every file of the snapshot through kb.ingest and reconcile. Returns what each step did."""
    db.assert_local(url)
    from kb import consumer, db as kb_db, reconcile, rules
    from kb.ingest import FileReader, sync_current
    from kb.loader import load_contract

    settings = _kb_settings(url, s, root)
    conn = kb_db.connect(url, s.kb)
    try:
        rules.seed(conn, settings.seed_path)
        conn.commit()
        load_contract(conn, settings.structured_dir)
        confidence = consumer._confidence_map(conn)
        counts = sync_current(conn, _snapshot_shas(root / "current"), FileReader(root), settings.structured_dir,
                              confidence, process=consumer._ingest)
        consumer.mail_source.release_copies(conn)
        conn.commit()
        result = reconcile.run(conn, "lab_seed", {"from": str(root)})
        conn.commit()
    finally:
        conn.close()
    return {"sync": counts, "reconcile": {k: len(v) for k, v in result["changes"].items()},
            "issues": result["issues"]}


def _lookahead_log() -> Path:
    return Path(os.environ.get("LOOKAHEAD_LOG") or PROJECT_ROOT / "data" / "lookahead" / "log.jsonl")


def _lookahead_count(path: Path) -> int:
    if not path.is_file():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip())


def status(url: str, s: Schemas) -> dict:
    """Counts of files, facts, believed values, open issues, mail and Lookahead records, and the scenario clock."""
    db.assert_local(url)
    from kb import db as kb_db
    from mail import db as mail_db
    from plan import clock, db as plan_db

    conn = kb_db.connect(url, s.kb)
    try:
        files = conn.execute("SELECT count(*) AS n FROM documents WHERE active").fetchone()["n"]
        facts = conn.execute("SELECT count(*) AS n FROM facts WHERE valid").fetchone()["n"]
        believed = conn.execute("SELECT count(*) AS n FROM believed").fetchone()["n"]
        open_issues = conn.execute("SELECT count(*) AS n FROM issues WHERE state = 'open'").fetchone()["n"]
    finally:
        conn.close()

    conn = mail_db.connect(url, s.mail)
    try:
        mail_counts = {r["direction"]: r["n"] for r in conn.execute(
            "SELECT direction, count(*) AS n FROM messages GROUP BY direction")}
    finally:
        conn.close()

    conn = plan_db.connect(url, s.plan)
    try:
        clock_row = clock.read(conn)
    finally:
        conn.close()

    conn = db.connect(url, s.lab)
    try:
        runs = conn.execute("SELECT count(*) AS n FROM runs").fetchone()["n"]
        injections = conn.execute("SELECT count(*) AS n FROM injections").fetchone()["n"]
    finally:
        conn.close()

    return {
        "files": files,
        "facts": facts,
        "believed": believed,
        "open_issues": open_issues,
        "mail": {"in": mail_counts.get("in", 0), "out": mail_counts.get("out", 0)},
        "lookahead_records": _lookahead_count(_lookahead_log()),
        "runs": runs,
        "injections": injections,
        "scenario_clock": {"mode": clock_row["mode"],
                           "scenario_now": clock_row["scenario_now"].isoformat() if clock_row["scenario_now"] else None,
                           "rate": float(clock_row["rate"])},
    }
