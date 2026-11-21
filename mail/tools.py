"""Tool functions for the planner and supplier-chaser agents. Each returns a JSON-serializable dict and never
raises into the agent. Approving, linking unknown senders and changing contacts are human-only (CLI / UI)."""

from __future__ import annotations

import functools
import json
import time
from datetime import datetime

from . import db, drafts, send as send_mod
from .config import MailSettings, load_settings
from .envelope import wrap_untrusted
from .transport import Sender


def _jsonable(obj):
    return json.loads(json.dumps(obj, default=str, ensure_ascii=False))


class MailTools:
    def __init__(self, settings: MailSettings | None = None, conn=None, sender=None, actor: str = "agent"):
        self.s = settings or load_settings()
        self.conn = conn or db.connect(self.s.require_db(), self.s.schema)
        self._sender = sender
        self.actor = actor

    @property
    def sender(self):
        if self._sender is None:
            self.s.require_credentials()
            self._sender = Sender(self.s.smtp_host, self.s.smtp_port, self.s.address, self.s.password)
        return self._sender

    @staticmethod
    def _safe(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            try:
                return _jsonable(fn(self, *args, **kwargs))
            except drafts.DraftError as exc:
                self.conn.rollback()
                return {"ok": False, "error": exc.code, "detail": exc.detail}
            except Exception as exc:
                self.conn.rollback()
                return {"ok": False, "error": "internal_error", "detail": f"{type(exc).__name__}: {exc}"[:500]}
        return wrapper

    # ---------------------------------------------------------------- read

    @_safe
    def list_contacts(self, supplier_id: str | None = None) -> dict:
        rows = self.conn.execute(
            "SELECT address, supplier_id, display_name, language, is_test FROM contacts WHERE revoked_at IS NULL "
            "AND send_allowed AND (%s::text IS NULL OR supplier_id = %s) ORDER BY supplier_id NULLS LAST, address",
            (supplier_id, supplier_id)).fetchall()
        self.conn.commit()
        return {"ok": True, "contacts": rows}

    @_safe
    def list_threads(self, supplier_id=None, status=None, po_number=None, since=None, limit: int = 20) -> dict:
        rows = self.conn.execute(
            "SELECT t.thread_id, t.tag, t.supplier_id, t.subject, t.status, t.po_numbers, t.last_message_at, "
            "(SELECT count(*) FROM messages m WHERE m.thread_id = t.thread_id) AS n_messages, "
            "(SELECT m.kind FROM messages m WHERE m.thread_id = t.thread_id AND m.direction = 'in' "
            " ORDER BY m.received_at DESC LIMIT 1) AS last_inbound_kind "
            "FROM threads t WHERE (%(s)s::text IS NULL OR t.supplier_id = %(s)s) "
            "AND (%(st)s::text IS NULL OR t.status = %(st)s) AND (%(po)s::text IS NULL OR %(po)s = ANY(t.po_numbers)) "
            "AND (%(since)s::timestamptz IS NULL OR t.last_message_at >= %(since)s) "
            "ORDER BY t.last_message_at DESC NULLS LAST, t.created_at DESC LIMIT %(lim)s",
            {"s": supplier_id, "st": status, "po": po_number, "since": since, "lim": min(int(limit), 200)}).fetchall()
        self.conn.commit()
        return {"ok": True, "threads": rows}

    def _message_view(self, m: dict, max_chars: int) -> dict:
        atts = self.conn.execute("SELECT id, filename, content_type, bytes, sha256, kb_path FROM attachments "
                                 "WHERE message_pk = %s ORDER BY id", (m["id"],)).fetchall()
        view = {k: m[k] for k in ("message_id", "direction", "kind", "from_addr", "to_addrs", "subject",
                                  "date_header", "received_at", "lang", "match_method", "trust", "flags")}
        view["attachments"] = atts
        if m["direction"] == "out":
            view["content"] = {"own_text": m["body_text"], "body_en": m["body_en"]}
        else:
            view["content"] = wrap_untrusted(m["body_text"], {"source": "email", "message_id": m["message_id"],
                                                              "from_addr": m["from_addr"], "trust": m["trust"]},
                                             max_chars)
            if m["body_en"]:
                view["body_en"] = m["body_en"]
        return view

    @_safe
    def get_thread(self, thread_id: str, max_chars_per_message: int = 4000) -> dict:
        t = self.conn.execute("SELECT * FROM threads WHERE thread_id = %s", (thread_id,)).fetchone()
        if not t:
            self.conn.commit()
            return {"ok": False, "error": "thread_not_found", "detail": thread_id}
        msgs = self.conn.execute("SELECT * FROM messages WHERE thread_id = %s ORDER BY COALESCE(date_header, "
                                 "received_at), id", (thread_id,)).fetchall()
        drafts_ = self.conn.execute("SELECT draft_id, status, subject, lang, created_at, created_by FROM drafts "
                                    "WHERE thread_id = %s ORDER BY created_at", (thread_id,)).fetchall()
        out = {"ok": True, "thread": t, "drafts": drafts_,
               "messages": [self._message_view(m, max_chars_per_message) for m in msgs]}
        self.conn.commit()
        return out

    @_safe
    def get_draft(self, draft_id: str) -> dict:
        d = drafts.load_draft(self.conn, draft_id)
        if not d:
            self.conn.commit()
            return {"ok": False, "error": "draft_not_found", "detail": draft_id}
        preview = drafts.render_mime(self.conn, d, self.s).as_string()
        self.conn.commit()
        return {"ok": True, "draft": d, "rendered_preview": preview[:6000]}

    @_safe
    def list_unmatched(self, since: str | None = None, limit: int = 50) -> dict:
        rows = self.conn.execute(
            "SELECT message_id, from_addr, from_name, subject, received_at, kind, flags FROM messages "
            "WHERE thread_id IS NULL AND direction = 'in' AND (%s::timestamptz IS NULL OR received_at >= %s) "
            "ORDER BY received_at DESC LIMIT %s", (since, since, min(int(limit), 200))).fetchall()
        self.conn.commit()
        return {"ok": True, "messages": rows}

    @_safe
    def poll_events(self, cursor: int | None = None, kinds: list[str] | None = None, limit: int = 50) -> dict:
        rows = self.conn.execute(
            "SELECT id, at, kind, payload FROM events WHERE id > %s AND (%s::text[] IS NULL OR kind = ANY(%s)) "
            "ORDER BY id LIMIT %s", (cursor or 0, kinds, kinds, min(int(limit), 500))).fetchall()
        self.conn.commit()
        return {"ok": True, "events": rows, "next_cursor": rows[-1]["id"] if rows else (cursor or 0)}

    # ---------------------------------------------------------------- write

    @_safe
    def draft_email(self, **fields) -> dict:
        allowed = {"supplier_id", "subject", "lang", "body_local", "body_en", "po_numbers", "cause", "thread_id",
                   "reply_to_message_id", "to", "cc", "lang_override_reason", "body_back_en"}
        unknown = set(fields) - allowed
        if unknown:
            return {"ok": False, "error": "unknown_fields", "detail": sorted(unknown)}
        result = drafts.create_draft(self.conn, self.s, created_by=self.actor, **fields)
        self.conn.commit()
        return result

    @_safe
    def cancel_draft(self, draft_id: str, reason: str) -> dict:
        cur = self.conn.execute("UPDATE drafts SET status = 'cancelled', updated_at = now() WHERE draft_id = %s "
                                "AND status IN ('draft', 'dry_run_sent', 'failed')", (draft_id,))
        db.audit(self.conn, self.actor, "draft_cancelled", "draft", draft_id, {"reason": reason})
        self.conn.commit()
        return {"ok": bool(cur.rowcount), **({} if cur.rowcount else {"error": "draft_not_cancellable"})}

    @_safe
    def send_approved(self, draft_id: str, approval_id: str) -> dict:
        sender = self.sender if self.s.send_mode != "dry_run" else None
        return send_mod.send_approved(self.conn, self.s, sender, draft_id, approval_id, actor=self.actor)

    # ---------------------------------------------------------------- wait

    def _latest_reply(self, thread_id: str, after: datetime | None, include_auto: bool):
        kinds = ["reply", "new", "bounce"] + (["auto_reply"] if include_auto else [])
        return self.conn.execute(
            "SELECT * FROM messages WHERE thread_id = %s AND direction = 'in' AND kind = ANY(%s) "
            "AND (%s::timestamptz IS NULL OR received_at > %s) ORDER BY received_at DESC, id DESC LIMIT 1",
            (thread_id, kinds, after, after)).fetchone()

    @_safe
    def wait_for_reply(self, thread_id: str, timeout_s: float = 120, include_auto_replies: bool = False,
                       max_chars: int = 4000) -> dict:
        timeout_s = max(0.0, min(float(timeout_s), 600.0))
        last_out = self.conn.execute("SELECT max(received_at) AS at FROM messages WHERE thread_id = %s AND "
                                     "direction = 'out'", (thread_id,)).fetchone()["at"]
        listen = db.connect(self.s.require_db(), self.s.schema, autocommit=True)
        try:
            listen.execute(f"LISTEN {db.NOTIFY_CHANNEL}")
            deadline = time.monotonic() + timeout_s
            while True:
                m = self._latest_reply(thread_id, last_out, include_auto_replies)
                self.conn.commit()
                if m:
                    status = "bounced" if m["kind"] == "bounce" else "replied"
                    return {"ok": True, "status": status, "message": self._message_view(m, max_chars)}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"ok": True, "status": "timeout", "thread_id": thread_id}
                for _ in listen.notifies(timeout=min(remaining, 30), stop_after=1):
                    pass
        finally:
            listen.close()
