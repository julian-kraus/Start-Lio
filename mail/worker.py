"""Poll loop: fetch new mail from All Mail and Spam, ingest it, resolve sends in unknown state, resync contacts."""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, timedelta

from tracker.store import BlobStore

from . import contacts, db, send
from .config import MailSettings
from .store import ingest
from .transport import Fetcher

log = logging.getLogger("mail")
POLL_FOLDERS = ("\\All", "\\Junk")
CONTACT_SYNC_EVERY = 600


def poll_once(conn, s: MailSettings, fetcher: Fetcher, blobs: BlobStore) -> dict:
    folders = fetcher.folders()
    counts = {"new": 0, "seen": 0, "failed": 0}
    for flag in POLL_FOLDERS:
        folder = folders.get(flag)
        if not folder:
            continue
        validity = fetcher.select(folder)
        state = conn.execute("SELECT uidvalidity, last_uid FROM sync_state WHERE folder = %s", (folder,)).fetchone()
        last = state["last_uid"] if state and state["uidvalidity"] == validity else None
        since = date.today() - timedelta(days=s.backfill_days if state is None else 7)
        for item in fetcher.fetch_new(last, since):
            try:
                result = ingest(conn, blobs, item.raw, s.address, s.tag_prefix, folder, item.uid, item.gm_msgid,
                                item.gm_thrid)
                counts["new" if result.new else "seen"] += 1
            except Exception as exc:
                conn.rollback()
                counts["failed"] += 1
                log.warning("ingest failed for %s uid %s: %s", flag, item.uid, type(exc).__name__)
                db.audit(conn, "mail:worker", "ingest_failed", "imap_uid", f"{folder}:{item.uid}",
                         {"error": f"{type(exc).__name__}: {exc}"[:500]})
            # The cursor moves in the same transaction as the message, so a crash re-fetches at most this one.
            conn.execute("INSERT INTO sync_state (folder, uidvalidity, last_uid) VALUES (%s,%s,%s) "
                         "ON CONFLICT (folder) DO UPDATE SET uidvalidity = EXCLUDED.uidvalidity, "
                         "last_uid = GREATEST(CASE WHEN sync_state.uidvalidity = EXCLUDED.uidvalidity "
                         "THEN sync_state.last_uid ELSE 0 END, EXCLUDED.last_uid), updated_at = now()",
                         (folder, validity, item.uid))
            conn.commit()
        if state is None or state["uidvalidity"] != validity:
            # Remember the folder even when it was empty, so the next poll is incremental.
            conn.execute("INSERT INTO sync_state (folder, uidvalidity, last_uid) VALUES (%s,%s,0) "
                         "ON CONFLICT (folder) DO NOTHING", (folder, validity))
            conn.commit()
    counts["resolved"] = send.resolve_unknown(conn, fetcher)
    conn.commit()
    return counts


def run(s: MailSettings, stop: threading.Event) -> None:
    s.require_credentials()
    conn = db.connect(s.require_db(), s.schema)
    blobs = BlobStore(s.data_dir)
    backoff = (5, 30, 120)
    failures, last_contacts = 0, 0.0
    try:
        while not stop.is_set():
            try:
                if time.monotonic() - last_contacts > CONTACT_SYNC_EVERY:
                    log.info("contacts: %s", contacts.sync(conn, s))
                    conn.commit()
                    last_contacts = time.monotonic()
                with Fetcher(s.imap_host, s.imap_port, s.address, s.password) as fetcher:
                    while not stop.is_set():
                        counts = poll_once(conn, s, fetcher, blobs)
                        if counts["new"] or counts["failed"] or counts["resolved"]:
                            log.info("poll: %s", counts)
                        failures = 0
                        stop.wait(s.poll_interval)
                        if time.monotonic() - last_contacts > CONTACT_SYNC_EVERY:
                            break
            except Exception as exc:
                conn.rollback()
                wait = backoff[min(failures, len(backoff) - 1)]
                failures += 1
                log.warning("mail loop error (%s), retrying in %ss", type(exc).__name__, wait)
                stop.wait(wait)
    finally:
        conn.close()
