"""The kb and mail workers survive a lost database connection.

Runs when TEST_DATABASE_URL points at a throwaway Postgres. Each worker holds a single-worker advisory lock;
killing its backend with pg_terminate_backend must make it back off, reconnect, re-take the lock and (for kb)
resume from the stored cursor. The tracker's tables live in public, as on a real database, also when kb's schema
holds empty copies of them. Synthetic data only."""

import os
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone

import pytest
from psycopg import sql

from kb import consumer, db, rules
from kb.config import KbSettings
from mail import config as mail_config
from mail import db as mail_db
from mail import worker as mail_worker
from mail.store import put_blob
from tracker import db as tracker_db
from tracker.store import BlobStore

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

TRACKER_TABLES = ("polls", "file_versions", "events", "stale_flags", "consumer_cursors")
SEED = """
rules: []
settings:
  confidence_map: {high: 0.9, medium: 0.75, low: 0.4}
"""


def _wait(predicate, message, timeout=20.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s: {message}")


@pytest.fixture
def kb_env(tmp_path):
    schema = f"kb_rc_{uuid.uuid4().hex[:8]}"
    conn = db.connect(URL, schema)
    db.migrate(conn, schema)
    seed = tmp_path / "seed.yaml"
    seed.write_text(SEED)
    rules.seed(conn, seed)
    conn.commit()
    data_dir = tmp_path / "data"
    structured = tmp_path / "structured"
    data_dir.mkdir()
    structured.mkdir()
    settings = KbSettings(database_url=URL, schema=schema, data_dir=data_dir, structured_dir=structured,
                          seed_path=seed, llm_adapter=None)
    watch = db.connect(URL, schema, autocommit=True)
    try:
        yield settings, conn, watch, schema
    finally:
        watch.close()
        conn.rollback()
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        conn.commit()
        conn.close()


class Portal:
    """What the tracker writes, over the tracker's own connection: versions and events in public."""

    def __init__(self, conn, data_dir):
        self.conn, self.blobs = conn, BlobStore(data_dir)
        self.prefix = f"synthetic/{uuid.uuid4().hex}/"
        self.poll_id = tracker_db.start_poll(conn)

    def put(self, event=True):
        """A new file. Without an event it is part of the baseline, which only the start-up sync reads."""
        path, data = f"{self.prefix}{uuid.uuid4().hex}.csv", f"a,b\n{uuid.uuid4().hex},1\n".encode()
        sha = put_blob(self.blobs, data)
        tracker_db.lock_events(self.conn)
        tracker_db.add_version(self.conn, path, sha, len(data), datetime.now(timezone.utc))
        event_id = None
        if event:
            event_id = tracker_db.insert_event(self.conn, self.poll_id, "added", path, "added", new_sha=sha)
        self.conn.commit()
        return path, event_id


@pytest.fixture
def portal(kb_env):
    settings = kb_env[0]
    with closing(tracker_db.connect(URL)) as conn:
        tracker_db.init_schema(conn)
        conn.execute("DELETE FROM consumer_cursors WHERE consumer = %s", (consumer.CONSUMER,))
        conn.commit()
        created = Portal(conn, settings.data_dir)
        yield created
        conn.rollback()
        conn.execute("DELETE FROM events WHERE poll_id = %s", (created.poll_id,))
        conn.execute("DELETE FROM file_versions WHERE starts_with(path, %s)", (created.prefix,))
        conn.execute("DELETE FROM polls WHERE id = %s", (created.poll_id,))
        conn.execute("DELETE FROM consumer_cursors WHERE consumer = %s", (consumer.CONSUMER,))
        conn.commit()


@pytest.fixture
def start_worker(kb_env, portal):
    running = []

    def start():
        stop = threading.Event()
        worker = threading.Thread(target=consumer.run, args=(kb_env[0], stop), kwargs={"idle_timeout": 0.1},
                                  daemon=True)
        worker.start()
        running.append((stop, worker))

    yield start
    for stop, worker in running:
        stop.set()
        worker.join(timeout=15)
        assert not worker.is_alive()


def _documents(watch):
    return {r["path"] for r in watch.execute("SELECT path FROM documents WHERE active")}


def _copies(watch, schema):
    return [t for t in TRACKER_TABLES
            if watch.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"{schema}.{t}",)).fetchone()["ok"]]


def _cursor(watch):
    row = watch.execute("SELECT last_event_id FROM public.consumer_cursors WHERE consumer = %s",
                        (consumer.CONSUMER,)).fetchone()
    return row["last_event_id"] if row else None


def _worker_pid(watch):
    row = watch.execute(
        "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND classid = %s AND objid = %s AND objsubid = 1",
        (consumer.WORKER_LOCK >> 32, consumer.WORKER_LOCK & 0xFFFFFFFF)).fetchone()
    return row["pid"] if row else None


def test_file_dropped_while_running_becomes_a_document(kb_env, portal, start_worker):
    _, _, watch, schema = kb_env
    baseline, _ = portal.put(event=False)
    start_worker()
    _wait(lambda: baseline in _documents(watch), "start-up sync did not finish")
    dropped, _ = portal.put()
    _wait(lambda: dropped in _documents(watch), "the dropped file did not become a document")
    assert _copies(watch, schema) == []


def test_file_drop_is_seen_when_kb_already_has_shadow_copies(kb_env, portal, start_worker):
    _, conn, watch, schema = kb_env
    tracker_db.init_schema(conn)   # on kb's search_path this creates the copies
    conn.execute("INSERT INTO consumer_cursors (consumer, last_event_id) VALUES (%s, 999999)", (consumer.CONSUMER,))
    conn.commit()
    assert _copies(watch, schema) == list(TRACKER_TABLES)
    baseline, _ = portal.put(event=False)
    start_worker()
    _wait(lambda: baseline in _documents(watch), "start-up sync did not finish")
    dropped, event_id = portal.put()
    _wait(lambda: dropped in _documents(watch), "the dropped file did not become a document")
    _wait(lambda: (_cursor(watch) or 0) >= event_id, "the cursor in public did not move")
    assert watch.execute("SELECT last_event_id FROM consumer_cursors").fetchone()["last_event_id"] == 999999


def test_file_dropped_during_the_startup_sync_is_not_skipped(kb_env, portal, start_worker, monkeypatch):
    _, _, watch, _ = kb_env
    real_sync, dropped = consumer.sync, []

    def sync_then_drop(*args, **kwargs):
        counts = real_sync(*args, **kwargs)
        if not dropped:
            dropped.append(portal.put()[0])   # too late for the sync, earlier than the first catch-up
        return counts

    monkeypatch.setattr(consumer, "sync", sync_then_drop)
    start_worker()
    _wait(lambda: dropped and dropped[0] in _documents(watch), "a drop during the start-up sync was skipped")


def test_kb_consumer_resumes_from_cursor_after_disconnect(kb_env, portal):
    settings, conn, watch, schema = kb_env
    stop = threading.Event()
    worker = threading.Thread(target=consumer.run, args=(settings, stop), kwargs={"idle_timeout": 0.1}, daemon=True)
    worker.start()
    try:
        _wait(lambda: _cursor(watch) is not None, "worker never initialised its cursor")
        # Only one worker follows the feed: a second lock attempt fails while the worker holds it.
        with closing(db.connect(URL, schema)) as other:
            assert consumer._take_lock(other) is False

        first = [portal.put() for _ in range(3)][-1][1]
        _wait(lambda: (_cursor(watch) or 0) >= first, "first batch was not consumed")

        pid = _worker_pid(watch)
        assert pid is not None
        with closing(db.connect(URL, schema, autocommit=True)) as killer:
            killer.execute("SELECT pg_terminate_backend(%s)", (pid,))

        # Events emitted after the disconnect must still be consumed, from the stored cursor, after reconnect.
        later = [portal.put() for _ in range(3)]
        second = later[-1][1]
        _wait(lambda: (_cursor(watch) or 0) >= second, "worker did not resume after reconnect")
        assert (_cursor(watch) or 0) >= second
        assert {path for path, _ in later} <= _documents(watch)

        # The reconnected worker re-took the lock.
        _wait(lambda: _worker_pid(watch) is not None, "worker did not re-take the lock")
        with closing(db.connect(URL, schema)) as other:
            assert consumer._take_lock(other) is False
    finally:
        stop.set()
        worker.join(timeout=15)
    assert not worker.is_alive()


def test_mail_worker_lock_is_exclusive_and_reclaimable(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("MAIL_SCHEMA", "public")
    s = mail_config.load_settings(load_files=False)

    held = mail_worker._connect_locked(s)
    assert held is not None
    try:
        assert mail_worker._connect_locked(s) is None  # a second worker cannot take it
        pid = held.execute("SELECT pg_backend_pid() AS p").fetchone()["p"]
        with closing(mail_db.connect(URL, "public", autocommit=True)) as killer:
            killer.execute("SELECT pg_terminate_backend(%s)", (pid,))

        reclaimed = None
        for _ in range(200):
            reclaimed = mail_worker._connect_locked(s)
            if reclaimed is not None:
                break
            time.sleep(0.05)
        assert reclaimed is not None  # the lock frees when the killed backend dies
        reclaimed.close()
    finally:
        mail_worker._close(held)
