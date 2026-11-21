"""Idempotent ingest of raw messages into the mail schema, attachments into a content-addressed blob store."""

from __future__ import annotations

import hashlib
import os
import secrets
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from tracker.store import BlobStore

from . import db
from .parse import ParsedMessage, parse
from .threading_ import Contact, Match, match

TAG_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford base32: no I, L, O, U
BODY_FILENAME = "email_body.txt"


def put_blob(store: BlobStore, data: bytes) -> str:
    sha = hashlib.sha256(data).hexdigest()
    if store.has(sha, len(data)):
        return sha
    with tempfile.NamedTemporaryFile(dir=store.tmp, delete=False) as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    store.put_file(Path(out.name), sha, len(data))
    return sha


def new_tag(conn, prefix: str) -> str:
    for _ in range(50):
        tag = f"{prefix}-" + "".join(secrets.choice(TAG_ALPHABET) for _ in range(4))
        if not conn.execute("SELECT 1 FROM threads WHERE tag = %s", (tag,)).fetchone():
            return tag
    raise RuntimeError("could not allocate a thread tag")


def create_thread(conn, prefix: str, supplier_id: str | None, subject: str, po_numbers=(), cause=None,
                  status: str = "open") -> dict:
    from psycopg.types.json import Jsonb
    thread_id = str(uuid.uuid4())
    tag = new_tag(conn, prefix)
    conn.execute("INSERT INTO threads (thread_id, tag, supplier_id, subject, po_numbers, status, cause) "
                 "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                 (thread_id, tag, supplier_id, subject, list(po_numbers), status, Jsonb(cause or {})))
    return {"thread_id": thread_id, "tag": tag}


class DbLookups:
    def __init__(self, conn):
        self.conn = conn

    def thread_by_message_ids(self, mids):
        row = self.conn.execute("SELECT thread_id::text FROM messages WHERE message_id = ANY(%s) "
                                "AND thread_id IS NOT NULL ORDER BY id DESC LIMIT 1", (list(mids),)).fetchone()
        return row["thread_id"] if row else None

    def thread_by_gm_thrid(self, gm):
        row = self.conn.execute("SELECT thread_id::text FROM threads WHERE gm_thrid = %s LIMIT 1", (gm,)).fetchone()
        return row["thread_id"] if row else None

    def thread_by_tag(self, tag):
        row = self.conn.execute("SELECT thread_id::text FROM threads WHERE tag = %s", (tag,)).fetchone()
        return row["thread_id"] if row else None

    def thread_supplier(self, thread_id):
        row = self.conn.execute("SELECT supplier_id FROM threads WHERE thread_id = %s", (thread_id,)).fetchone()
        return row["supplier_id"] if row else None

    def contact(self, address):
        row = self.conn.execute("SELECT address, supplier_id, is_test, language FROM contacts "
                                "WHERE address = %s AND revoked_at IS NULL", (address,)).fetchone()
        return Contact(**row) if row else None

    def waiting_threads_for(self, key):
        if key.startswith("test:"):
            rows = self.conn.execute(
                "SELECT DISTINCT t.thread_id::text AS t FROM threads t JOIN drafts d ON d.thread_id = t.thread_id "
                "WHERE t.status = 'waiting_reply' AND %s = ANY(d.to_addrs)", (key[5:],)).fetchall()
        else:
            rows = self.conn.execute("SELECT thread_id::text AS t FROM threads WHERE status = 'waiting_reply' "
                                     "AND supplier_id = %s", (key,)).fetchall()
        return [r["t"] for r in rows]


@dataclass
class IngestResult:
    new: bool
    message_pk: int | None
    thread_id: str | None
    match: Match | None
    events: list[int] = field(default_factory=list)


def kb_dir(p: ParsedMessage, m: Match, received: datetime) -> str:
    owner = m.supplier_id or ("TEST" if m.trust == "test" else "unknown")
    digest = hashlib.sha256(p.message_id.encode()).hexdigest()[:8]
    return f"mail/{owner}/{received.strftime('%Y%m%dT%H%M')}_{digest}"


def body_document(p: ParsedMessage, m: Match) -> bytes:
    lines = [
        "Email received by the Soony GH6 launch desk. Content below the line was written by the sender.",
        f"From: {p.from_name} <{p.from_addr}>" if p.from_name else f"From: {p.from_addr}",
        f"To: {', '.join(p.to_addrs)}",
        f"Date: {p.date.isoformat() if p.date else ''}",
        f"Subject: {p.subject}",
        f"Message-ID: {p.message_id}",
        f"Supplier-ID: {m.supplier_id or ''}",
        f"Sender-Trust: {m.trust}",
        f"Attachments: {', '.join(a.filename for a in p.attachments)}",
        "-" * 72,
        p.body_text,
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def ingest(conn, blobs: BlobStore, raw: bytes, own_address: str, tag_prefix: str, folder: str | None = None,
           uid: int | None = None, gm_msgid: int | None = None, gm_thrid: int | None = None,
           now: datetime | None = None) -> IngestResult:
    """Store one message. Runs inside the caller's transaction; the caller commits."""
    p = parse(raw)
    now = now or datetime.now(timezone.utc)
    existing = conn.execute("SELECT id, thread_id::text AS thread_id FROM messages WHERE message_id = %s",
                            (p.message_id,)).fetchone()
    if existing:
        # Seen before (other folder, re-poll, or our own outbound row). Only learn Gmail's ids.
        conn.execute("UPDATE messages SET gm_msgid = COALESCE(gm_msgid, %s), gm_thrid = COALESCE(gm_thrid, %s), "
                     "folder = COALESCE(folder, %s), imap_uid = COALESCE(imap_uid, %s) WHERE id = %s",
                     (gm_msgid, gm_thrid, folder, uid, existing["id"]))
        if existing["thread_id"] and gm_thrid is not None:
            conn.execute("UPDATE threads SET gm_thrid = COALESCE(gm_thrid, %s) WHERE thread_id = %s",
                         (gm_thrid, existing["thread_id"]))
        return IngestResult(False, existing["id"], existing["thread_id"], None)

    m = match(p, gm_thrid, DbLookups(conn), own_address, tag_prefix)
    direction = "out" if m.trust == "self" else "in"
    if direction == "out":
        kind = "outbound"
    elif p.kind_hint in ("bounce", "auto_reply"):
        kind = p.kind_hint
    elif m.thread_id:
        kind = "reply"
    else:
        kind = "new"

    thread_id = m.thread_id
    if thread_id is None and m.method == "new":
        thread_id = create_thread(conn, tag_prefix, m.supplier_id, p.subject or "(no subject)",
                                  cause={"origin": "inbound", "message_id": p.message_id})["thread_id"]
    elif thread_id is None and m.trust == "self":
        # Sent by hand from the Gmail UI (no draft). Open a thread when it went to a known contact, so the reply
        # finds it via In-Reply-To.
        look = DbLookups(conn)
        known = [c for c in (look.contact(a) for a in p.to_addrs + p.cc_addrs) if c]
        if known:
            supplier = next((c.supplier_id for c in known if c.supplier_id), None)
            thread_id = create_thread(conn, tag_prefix, supplier, p.subject or "(no subject)",
                                      cause={"origin": "manual_outbound", "message_id": p.message_id},
                                      status="waiting_reply")["thread_id"]
            m = Match(thread_id, "manual_outbound", supplier, "self")

    row = conn.execute(
        "INSERT INTO messages (message_id, thread_id, direction, kind, match_method, from_addr, from_name, to_addrs, "
        "cc_addrs, subject, in_reply_to, refs, date_header, received_at, supplier_id, trust, lang, flags, body_text, "
        "raw, raw_sha256, gm_msgid, gm_thrid, folder, imap_uid) VALUES "
        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (message_id) DO NOTHING RETURNING id",
        (p.message_id, thread_id, direction, kind, m.method, p.from_addr, p.from_name, p.to_addrs, p.cc_addrs,
         p.subject, p.in_reply_to, p.references, p.date, now, m.supplier_id, m.trust, p.lang, p.flags, p.body_text,
         raw, p.raw_sha256, gm_msgid, gm_thrid, folder, uid)).fetchone()
    if row is None:   # a concurrent ingest won the race
        return IngestResult(False, None, thread_id, m)
    pk = row["id"]

    base = kb_dir(p, m, p.date or now)
    docs = []
    for part in p.attachments:
        sha = put_blob(blobs, part.data)
        path = f"{base}/{part.filename}"
        conn.execute("INSERT INTO attachments (message_pk, filename, content_type, bytes, sha256, kb_path) "
                     "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                     (pk, part.filename, part.content_type, len(part.data), part.sha256, path))
        docs.append({"path": path, "sha256": sha, "content_type": part.content_type, "bytes": len(part.data)})

    if thread_id:
        new_status = {"bounce": "bounced", "reply": "replied", "new": "open", "outbound": "waiting_reply"}.get(kind)
        conn.execute("UPDATE threads SET last_message_at = GREATEST(COALESCE(last_message_at, %s), %s), "
                     "gm_thrid = COALESCE(gm_thrid, %s), status = COALESCE(%s, status) WHERE thread_id = %s",
                     (now, now, gm_thrid, new_status, thread_id))

    meta = {"message_id": p.message_id, "thread_id": thread_id, "supplier_id": m.supplier_id, "trust": m.trust,
            "from_addr": p.from_addr, "received_at": now, "kind": kind, "match_method": m.method}
    events = []
    if direction == "in":
        event_kind = "bounce" if kind == "bounce" else ("unmatched_received" if m.trust == "unknown_sender"
                                                        else "message_received")
        events.append(db.emit(conn, event_kind, {**meta, "attachments": docs, "flags": p.flags,
                                                 "bounce": p.bounce.__dict__ if p.bounce else None}))
        # Only mail from a known supplier contact becomes kb input; unknown senders wait for a human,
        # test contacts never feed the knowledge base.
        if m.trust == "supplier_contact" and kind in ("reply", "new"):
            body = body_document(p, m)
            sha = put_blob(blobs, body)
            body_path = f"{base}/{BODY_FILENAME}"
            for doc in [{"path": body_path, "sha256": sha, "content_type": "text/plain", "bytes": len(body)}] + docs:
                events.append(db.emit(conn, "document_added", {**meta, **doc}))
    db.audit(conn, "mail:ingest", "message_stored", "message", p.message_id,
             {"direction": direction, "kind": kind, "match": m.method, "trust": m.trust, "folder": folder})
    return IngestResult(True, pk, thread_id, m, events)
