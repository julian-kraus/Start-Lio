"""CLI: python -m mail <command>."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from pathlib import Path

from tracker.store import BlobStore

from . import contacts, db, drafts, worker
from .config import load_settings
from .tools import MailTools
from .transport import Fetcher, Sender

CHECKLIST = """Checklist before the first live supplier email:
  [ ] Gmail address registered as sender in the Soony portal Settings (then MAIL_SENDER_REGISTERED=yes)
  [ ] Gmail filter: from:(@<supplier domain>) -> Never send it to Spam
  [ ] Smoke test in MAIL_SEND_MODE=test_only to a team address (MAIL_TEST_ADDRESSES)
  [ ] Explicit go-ahead from the team for the first real supplier email, then MAIL_SEND_MODE=live"""


def out(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def cmd_init(a, s, conn):
    print(f"schema {s.schema}: applied {db.migrate(conn, s.schema) or 'nothing (up to date)'}")


def cmd_check(a, s, conn):
    s.require_credentials()
    print(f"account: {s.address}  password: ****  mode: {s.send_mode}  sender_registered: {s.sender_registered}")
    with Fetcher(s.imap_host, s.imap_port, s.address, s.password) as f:
        print(f"IMAP ok, folders: {json.dumps(f.folders(), ensure_ascii=False)}")
    Sender(s.smtp_host, s.smtp_port, s.address, s.password).check()
    print("SMTP login ok (nothing sent)")
    if conn is not None:
        n = conn.execute("SELECT count(*) AS n FROM contacts WHERE revoked_at IS NULL").fetchone()["n"]
        print(f"active contacts: {n}  sending_enabled: {db.get_setting(conn, 'sending_enabled', True)}")
    print(CHECKLIST)


def cmd_sync_contacts(a, s, conn):
    out(contacts.sync(conn, s, actor=f"cli:{a.by}"))
    conn.commit()


def cmd_contacts(a, s, conn):
    out(conn.execute("SELECT address, supplier_id, language, is_test, source, revoked_at FROM contacts "
                     "ORDER BY revoked_at NULLS FIRST, supplier_id NULLS LAST, address").fetchall())


def cmd_add_contact(a, s, conn):
    contacts.add_manual(conn, a.address, a.supplier, a.test, f"cli:{a.by}", a.language)
    conn.commit()
    print("added")


def cmd_revoke_contact(a, s, conn):
    print("revoked" if contacts.revoke(conn, a.address, f"cli:{a.by}") else "not found or already revoked")
    conn.commit()


def cmd_poll(a, s, conn):
    s.require_credentials()
    with Fetcher(s.imap_host, s.imap_port, s.address, s.password) as f:
        out(worker.poll_once(conn, s, f, BlobStore(s.data_dir)))


def cmd_run(a, s, conn):
    conn.close()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    worker.run(s, stop)


def _tools(a, s, conn, actor="cli"):
    return MailTools(s, conn, actor=actor)


def cmd_draft(a, s, conn):
    fields = json.loads(Path(a.file).read_text())
    out(_tools(a, s, conn, actor=f"cli:{a.by}").draft_email(**fields))


def cmd_show_draft(a, s, conn):
    out(_tools(a, s, conn).get_draft(a.draft_id))


def cmd_cancel_draft(a, s, conn):
    out(_tools(a, s, conn, actor=f"cli:{a.by}").cancel_draft(a.draft_id, a.reason))


def cmd_approve(a, s, conn):
    result = drafts.approve(conn, a.draft_id, a.as_, "rejected" if a.reject else "approved", a.role, a.policy)
    conn.commit()
    out(result)


def cmd_send(a, s, conn):
    out(_tools(a, s, conn, actor=f"cli:{a.by}").send_approved(a.draft_id, a.approval))


def cmd_threads(a, s, conn):
    out(_tools(a, s, conn).list_threads(supplier_id=a.supplier, status=a.status, limit=a.limit))


def cmd_thread(a, s, conn):
    out(_tools(a, s, conn).get_thread(a.thread_id))


def cmd_unmatched(a, s, conn):
    out(_tools(a, s, conn).list_unmatched())


def cmd_link(a, s, conn):
    m = conn.execute("SELECT id, from_addr FROM messages WHERE message_id = %s AND direction = 'in'",
                     (a.message_id,)).fetchone()
    if not m:
        raise SystemExit("no such inbound message")
    thread = a.thread
    if not thread:
        from .store import create_thread
        thread = create_thread(conn, s.tag_prefix, a.supplier, "(linked by hand)",
                               cause={"origin": "manual_link", "by": a.by})["thread_id"]
    conn.execute("UPDATE messages SET thread_id = %s, supplier_id = COALESCE(%s, supplier_id), match_method = "
                 "'manual' WHERE id = %s", (thread, a.supplier, m["id"]))
    db.audit(conn, f"cli:{a.by}", "message_linked", "message", a.message_id, {"thread_id": thread,
                                                                             "supplier_id": a.supplier})
    conn.commit()
    print(f"linked to thread {thread}. The sender is NOT added as a contact; use add-contact if that is intended.")


def cmd_pause(a, s, conn):
    db.set_setting(conn, "sending_enabled", False)
    db.audit(conn, f"cli:{a.by}", "sending_paused", "settings", "sending_enabled")
    conn.commit()
    print("sending paused")


def cmd_resume(a, s, conn):
    db.set_setting(conn, "sending_enabled", True)
    db.audit(conn, f"cli:{a.by}", "sending_resumed", "settings", "sending_enabled")
    conn.commit()
    print("sending resumed")


def cmd_status(a, s, conn):
    out({"mode": s.send_mode, "sender_registered": s.sender_registered,
         "sending_enabled": db.get_setting(conn, "sending_enabled", True),
         "counts": conn.execute(
             "SELECT (SELECT count(*) FROM contacts WHERE revoked_at IS NULL) AS contacts, "
             "(SELECT count(*) FROM threads) AS threads, (SELECT count(*) FROM messages) AS messages, "
             "(SELECT count(*) FROM drafts WHERE status = 'draft') AS open_drafts, "
             "(SELECT count(*) FROM sends WHERE state = 'sent') AS sent, "
             "(SELECT count(*) FROM messages WHERE thread_id IS NULL AND direction = 'in') AS unmatched").fetchone(),
         "sync_state": conn.execute("SELECT * FROM sync_state ORDER BY folder").fetchall()})


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="python -m mail")
    p.add_argument("--by", default="team", help="who runs this command (recorded in the audit log)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("check")
    sub.add_parser("sync-contacts")
    sub.add_parser("contacts")
    x = sub.add_parser("add-contact")
    x.add_argument("address")
    g = x.add_mutually_exclusive_group(required=True)
    g.add_argument("--test", action="store_true")
    g.add_argument("--supplier")
    x.add_argument("--language")
    sub.add_parser("revoke-contact").add_argument("address")
    x = sub.add_parser("poll")
    x.add_argument("--once", action="store_true", default=True)
    sub.add_parser("run")
    sub.add_parser("draft").add_argument("file")
    sub.add_parser("show-draft").add_argument("draft_id")
    x = sub.add_parser("cancel-draft")
    x.add_argument("draft_id")
    x.add_argument("--reason", required=True)
    x = sub.add_parser("approve")
    x.add_argument("draft_id")
    x.add_argument("--as", dest="as_", required=True, help="name of the person approving")
    x.add_argument("--role")
    x.add_argument("--policy")
    x.add_argument("--reject", action="store_true")
    x = sub.add_parser("send")
    x.add_argument("draft_id")
    x.add_argument("--approval", required=True)
    x = sub.add_parser("threads")
    x.add_argument("--supplier")
    x.add_argument("--status")
    x.add_argument("--limit", type=int, default=20)
    sub.add_parser("thread").add_argument("thread_id")
    sub.add_parser("unmatched")
    x = sub.add_parser("link")
    x.add_argument("message_id")
    g = x.add_mutually_exclusive_group(required=True)
    g.add_argument("--thread")
    g.add_argument("--supplier")
    sub.add_parser("pause")
    sub.add_parser("resume")
    sub.add_parser("status")
    a = p.parse_args(argv)

    s = load_settings()
    conn = None
    if s.database_url:
        conn = db.connect(s.database_url, s.schema)
        if a.cmd != "init":
            db.migrate(conn, s.schema)
    elif a.cmd != "check":
        s.require_db()
    try:
        globals()["cmd_" + a.cmd.replace("-", "_")](a, s, conn)
    finally:
        if conn is not None and not conn.closed:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
