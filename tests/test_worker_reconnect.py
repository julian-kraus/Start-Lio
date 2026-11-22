"""The kb and mail workers survive a lost database connection.

Runs when TEST_DATABASE_URL points at a throwaway Postgres. Each worker holds a single-worker advisory lock;
killing its backend with pg_terminate_backend must make it back off, reconnect, re-take the lock and (for kb)
resume from the stored cursor. Synthetic data only."""

import os
import threading
import time
import uuid
from contextlib import closing

import pytest
from psycopg import sql

from kb import consumer, db, rules
from kb.config import KbSettings
from mail import config as mail_config
from mail import db as mail_db
from mail import worker as mail_worker
from tracker import db as tracker_db

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

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
    tracker_db.init_schema(conn)
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


def _cursor(watch):
    row = watch.execute("SELECT last_event_id FROM consumer_cursors WHERE consumer = %s",
                        (consumer.CONSUMER,)).fetchone()
    return row["last_event_id"] if row else None


def _worker_pid(watch):
    row = watch.execute(
        "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND classid = %s AND objid = %s AND objsubid = 1",
        (consumer.WORKER_LOCK >> 32, consumer.WORKER_LOCK & 0xFFFFFFFF)).fetchone()
    return row["pid"] if row else None


def _emit(conn, poll_id, n):
    last = 0
    for _ in range(n):
        last = tracker_db.insert_event(conn, poll_id, "added", f"synthetic/{uuid.uuid4().hex}.csv", "added",
                                       new_sha=uuid.uuid4().hex)
    conn.commit()
    return last


def test_kb_consumer_resumes_from_cursor_after_disconnect(kb_env):
    settings, conn, watch, schema = kb_env
    stop = threading.Event()
    worker = threading.Thread(target=consumer.run, args=(settings, stop), kwargs={"idle_timeout": 0.1}, daemon=True)
    worker.start()
    try:
        _wait(lambda: _cursor(watch) is not None, "worker never initialised its cursor")
        # Only one worker follows the feed: a second lock attempt fails while the worker holds it.
        with closing(db.connect(URL, schema)) as other:
            assert consumer._take_lock(other) is False

        poll_id = tracker_db.start_poll(conn)
        first = _emit(conn, poll_id, 3)
        _wait(lambda: (_cursor(watch) or 0) >= first, "first batch was not consumed")

        pid = _worker_pid(watch)
        assert pid is not None
        with closing(db.connect(URL, schema, autocommit=True)) as killer:
            killer.execute("SELECT pg_terminate_backend(%s)", (pid,))

        # Events emitted after the disconnect must still be consumed, from the stored cursor, after reconnect.
        second = _emit(conn, poll_id, 3)
        _wait(lambda: (_cursor(watch) or 0) >= second, "worker did not resume after reconnect")
        assert (_cursor(watch) or 0) >= second

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
