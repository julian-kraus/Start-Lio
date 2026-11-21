"""Regression tests for the audit findings: unsafe paths, bad downloads, per-file isolation,
single poller, event ordering, and folding an older snapshot into the history."""

import hashlib
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_pdf
from test_integration import DB_URL, TOKEN, Portal, conn  # noqa: F401  (fixture)
from tracker import db, poller
from tracker.__main__ import main
from tracker.client import PortalClient
from tracker.config import Settings
from tracker.store import BlobStore

pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")


def setup(portal, tmp_path):
    settings = Settings(base_url=portal.url, token=TOKEN, database_url=DB_URL, data_dir=tmp_path,
                        poll_interval=1, stale_rules=(), webhook_urls=(), download_workers=4, diff_max_items=500)
    return settings, PortalClient(portal.url, TOKEN, retries=0), BlobStore(tmp_path)


def test_unsafe_path_is_skipped_and_never_wedges(conn, tmp_path):
    portal = Portal({"ok.csv": b"id,v\n1,a\n"})
    portal.extra_listing = [{"path": "../escape.csv", "bytes": 3, "sha256": "0" * 64, "url": ""},
                            {"path": "/abs.csv", "bytes": 3, "sha256": "1" * 64, "url": ""}]
    settings, client, store = setup(portal, tmp_path)
    result = poller.poll_once(settings, client, store, conn, baseline=True)
    assert {f["path"] for f in result.failed} == {"../escape.csv", "/abs.csv"}
    assert set(db.current_state(conn)) == {"ok.csv"}
    assert not (tmp_path.parent / "escape.csv").exists()
    conn.commit()
    portal.files["new.csv"] = b"id\n1\n"
    second = poller.poll_once(settings, client, store, conn)
    assert [e["path"] for e in second.events] == ["new.csv"]
    assert (tmp_path / "current/new.csv").exists()
    assert poller.reconcile_current(store, db.current_state(conn)) == 0  # restart is not wedged


def test_truncated_or_mismatched_download_is_never_recorded(conn, tmp_path):
    good = b"id,qty\n" + b"".join(b"%d,%d\n" % (i, i) for i in range(200))
    portal = Portal({"a.csv": b"id,qty\n1,1\n", "b.csv": b"x\n"})
    settings, client, store = setup(portal, tmp_path)
    poller.poll_once(settings, client, store, conn, baseline=True)
    portal.files["a.csv"] = good
    portal.served["a.csv"] = good[: len(good) // 2]   # cut short
    portal.files["b.csv"] = b"x\ny\n"
    portal.served["b.csv"] = b"x\nz\n"                  # same size, wrong checksum
    for _ in range(3):
        result = poller.poll_once(settings, client, store, conn)
        assert result.events == [] and {f["path"] for f in result.failed} == {"a.csv", "b.csv"}
    versions = conn.execute("SELECT path, count(*) AS n FROM file_versions GROUP BY path").fetchall()
    assert {r["path"]: r["n"] for r in versions} == {"a.csv": 1, "b.csv": 1}
    assert not any((tmp_path / "tmp").iterdir())  # no temp files leaked
    portal.served.clear()
    fixed = poller.poll_once(settings, client, store, conn)
    assert sorted(e["path"] for e in fixed.events) == ["a.csv", "b.csv"] and not fixed.failed


def test_one_bad_file_does_not_block_the_others(conn, tmp_path):
    portal = Portal({"gone.pdf": make_pdf(["x"]), "deep.json": b"[]", "ok.csv": b"id\n1\n"})
    settings, client, store = setup(portal, tmp_path)
    poller.poll_once(settings, client, store, conn, baseline=True)
    portal.files["gone.pdf"] = make_pdf(["y"])
    portal.missing.add("gone.pdf")                              # persistent 404
    portal.files["deep.json"] = ("[" * 50000 + "]" * 50000).encode()  # RecursionError bait
    portal.files["ok.csv"] = b"id\n2\n"
    result = poller.poll_once(settings, client, store, conn)
    kinds = {e["path"]: e["kind"] for e in result.events}
    assert kinds == {"deep.json": "changed", "ok.csv": "changed"}
    assert [f["path"] for f in result.failed] == ["gone.pdf"]
    assert db.current_state(conn)["gone.pdf"].version == 1  # unchanged, retried next poll


def test_only_one_poller(conn, tmp_path, monkeypatch):
    portal = Portal({"a.csv": b"id\n1\n"})
    monkeypatch.setenv("SOONY_API_BASE_URL", portal.url)
    monkeypatch.setenv("SOONY_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    monkeypatch.setenv("TRACKER_DATA_DIR", str(tmp_path))
    holder = db.connect(DB_URL)
    assert db.try_poller_lock(holder)
    holder.commit()
    assert main(["run", "--once"]) == 2       # refuses while another poller holds the lock
    holder.close()
    assert main(["run", "--once"]) == 0       # lock released with the connection


def test_event_ids_commit_in_order(conn):
    """A writer that started later must not commit an event id below one still in flight."""
    a, b = db.connect(DB_URL), db.connect(DB_URL)
    poll_id = db.start_poll(a)
    db.lock_events(a)
    first = db.insert_event(a, poll_id, "added", "a", "first")
    result = {}

    def writer_b():
        db.lock_events(b)  # blocks until a commits
        result["id"] = db.insert_event(b, poll_id, "added", "b", "second")
        b.commit()

    t = threading.Thread(target=writer_b)
    t.start()
    time.sleep(0.5)
    assert "id" not in result                 # b is waiting, not racing ahead
    a.commit()
    t.join(5)
    assert result["id"] > first
    a.close()
    b.close()


def test_import_history_folds_in_an_older_snapshot(conn, tmp_path):
    old_csv = b"product_id,weekly_units\nFG-1,100\nFG-2,50\n"
    new_csv = b"product_id,weekly_units,day_one_qty\nFG-1,100,640000\nFG-2,60,72000\n"
    same = b"site_id\nDC-HAM\n"
    removed = b"id\ngone\n"
    snapshot_dir = tmp_path / "snapshot"
    for name, data in {"p.csv": old_csv, "s.csv": same, "r.csv": removed}.items():
        (snapshot_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (snapshot_dir / name).write_bytes(data)
    taken = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    snapshot = {name: {"sha256": hashlib.sha256(d).hexdigest(), "bytes": len(d), "first_seen": taken}
                for name, d in {"p.csv": old_csv, "s.csv": same, "r.csv": removed}.items()}

    portal = Portal({"p.csv": new_csv, "s.csv": same, "memo.pdf": make_pdf(["CE first"])})
    settings, client, store = setup(portal, tmp_path / "data")
    poller.poll_once(settings, client, store, conn, baseline=True)
    conn.commit()

    result = poller.import_history(store, conn, snapshot, snapshot_dir, "first download")
    kinds = {e["path"]: e["kind"] for e in result.events}
    assert kinds == {"p.csv": "changed", "r.csv": "removed", "memo.pdf": "added"}
    changed = conn.execute("SELECT diff FROM events WHERE path = 'p.csv'").fetchone()["diff"]
    assert changed["rows_changed"] == [{"key": "FG-2", "changes": {"weekly_units": ["50", "60"]}}]
    assert changed["history_import"] == "first download"
    hist = conn.execute("SELECT version, sha256, superseded_at FROM file_versions WHERE path = 'p.csv' "
                        "ORDER BY version").fetchall()
    assert [h["sha256"] for h in hist] == [snapshot["p.csv"]["sha256"], hashlib.sha256(new_csv).hexdigest()]
    assert hist[0]["superseded_at"] is not None and hist[1]["superseded_at"] is None
    s_first = conn.execute("SELECT first_seen FROM file_versions WHERE path = 's.csv'").fetchone()["first_seen"]
    assert s_first == datetime.fromisoformat(taken)  # unchanged file: it existed since the snapshot
    conn.commit()
    with pytest.raises(poller.AlreadyImported):
        poller.import_history(store, conn, snapshot, snapshot_dir, "first download")
    conn.rollback()
    # Live polling continues normally on top of the imported history.
    portal.files["p.csv"] = new_csv + b"FG-3,1,1\n"
    nxt = poller.poll_once(settings, client, store, conn)
    assert [e["kind"] for e in nxt.events] == ["changed"]
    assert db.current_state(conn)["p.csv"].version == 3


def test_export_writes_events_and_before_after_files(conn, tmp_path):
    from tracker.export import export

    portal = Portal({"a.csv": b"id,v\n1,a\n"})
    settings, client, store = setup(portal, tmp_path / "data")
    poller.poll_once(settings, client, store, conn, baseline=True)
    portal.files["a.csv"] = b"id,v\n1,b\n"
    poller.poll_once(settings, client, store, conn)
    stats = export(conn, store, tmp_path / "out")
    assert stats["events"] == 1 and stats["files_tracked"] == 1
    events = json.loads((tmp_path / "out/events.json").read_text())
    assert events[0]["diff"]["rows_changed"][0]["changes"] == {"v": ["a", "b"]}
    assert (tmp_path / "out" / events[0]["files"]["before"]).read_bytes() == b"id,v\n1,a\n"
    assert (tmp_path / "out" / events[0]["files"]["after"]).read_bytes() == b"id,v\n1,b\n"
    assert (tmp_path / "out/inventory.csv").read_text().splitlines()[1].startswith("a.csv,a.csv,,csv,")
