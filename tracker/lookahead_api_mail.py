"""Routes of the Email log view of /lookahead/v2. Read-only.

GET /api/lookahead/mail           every stored message, newest first, with its thread's draft ids
GET /api/lookahead/mail/strings   the view's labels in English and Chinese (lookahead_static/strings/mail.json)

Mail is read only through the read methods of `mail.tools.MailTools`, behind `ReadOnlyMail`, which exposes
nothing else: no draft, approve, cancel or send path is reachable from here.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .lookahead import LookaheadError

log = logging.getLogger("tracker.lookahead")

PREFIX = "/api/lookahead/mail"
STRINGS = Path(__file__).with_name("lookahead_static") / "strings" / "mail.json"
ACTOR = "screen:mail-log"
MAX_THREADS = 200
MAX_UNMATCHED = 200
READ_METHODS = ("list_contacts", "list_threads", "get_thread", "list_unmatched")
_ENVELOPE = re.compile(r"\A<<<EMAIL_CONTENT id=\w+>>>\n(.*)\n<<<END_EMAIL_CONTENT id=\w+>>>\Z", re.S)


class ReadOnlyMail:
    """The read methods of a MailTools, and nothing else."""

    def __init__(self, tools):
        self._tools = tools
        for name in READ_METHODS:
            setattr(self, name, getattr(tools, name))

    def close(self) -> None:
        conn = getattr(self._tools, "conn", None)
        if conn is not None:
            conn.close()


def _ok(result: dict, key: str) -> list:
    if not result.get("ok"):
        raise LookaheadError(503, f"mail store: {result.get('error') or 'unavailable'}")
    return result.get(key) or []


def body_text(view: dict) -> str | None:
    """The message text for a person to read. Inbound text arrives wrapped for agents; the screen shows it as text."""
    content = view.get("content") or {}
    if view.get("direction") == "out":
        return content.get("own_text")
    wrapped = (content.get("untrusted_content") or {}).get("text")
    if wrapped is None:
        return None
    m = _ENVELOPE.match(wrapped)
    return m.group(1) if m else wrapped


def _person(address: str | None, contacts: dict) -> dict:
    c = contacts.get((address or "").lower()) or {}
    return {"address": address, "name": c.get("display_name"), "supplier_id": c.get("supplier_id")}


def _entry(view: dict, thread: dict | None, draft_ids: list[str], contacts: dict) -> dict:
    out = view.get("direction") == "out"
    to = list(view.get("to_addrs") or [])
    other = to[0] if out and to else view.get("from_addr")
    person = _person(other, contacts)
    return {
        "message_id": view.get("message_id"),
        "direction": "out" if out else "in",
        "at": view.get("date_header") or view.get("received_at"),
        "from": view.get("from_addr"),
        "from_name": view.get("from_name"),
        "to": to,
        "contact": person,
        "supplier_id": (thread or {}).get("supplier_id") or person["supplier_id"],
        "subject": view.get("subject"),
        "body": body_text(view),
        "body_en": view.get("body_en") or (view.get("content") or {}).get("body_en"),
        "lang": view.get("lang"),
        "attachments": [a.get("filename") for a in view.get("attachments") or [] if a.get("filename")],
        "thread_id": (thread or {}).get("thread_id"),
        "draft_ids": draft_ids,
    }


def mail_log(mail: ReadOnlyMail) -> dict:
    contacts = {str(c["address"]).lower(): c for c in _ok(mail.list_contacts(), "contacts")}
    entries = []
    for t in _ok(mail.list_threads(limit=MAX_THREADS), "threads"):
        full = mail.get_thread(str(t["thread_id"]))
        if not full.get("ok"):
            log.warning("mail log: thread %s not readable: %s", t["thread_id"], full.get("error"))
            continue
        thread = full["thread"]
        draft_ids = [str(d["draft_id"]) for d in full.get("drafts") or []]
        entries += [_entry(m, thread, draft_ids, contacts) for m in full.get("messages") or []]
    for m in _ok(mail.list_unmatched(limit=MAX_UNMATCHED), "messages"):
        entries.append(_entry({**m, "direction": "in", "content": None}, None, [], contacts))
    entries.sort(key=lambda e: str(e["at"] or ""), reverse=True)
    return {"ok": True, "available": True, "messages": entries}


def open_mail(routes) -> ReadOnlyMail | None:
    """MailTools on the screen's stores (tests pass theirs), or None when no mail database is configured."""
    from . import lookahead_decisions as decisions

    try:
        stores = routes.decision_stores or decisions.Stores()
        return ReadOnlyMail(stores.mail(ACTOR))
    except LookaheadError:
        return None
    except Exception as exc:  # no reachable database: the view shows its empty state
        log.warning("mail log: mail store not reachable: %s", type(exc).__name__)
        return None


def strings() -> dict:
    return json.loads(STRINGS.read_text(encoding="utf-8"))


def handle(routes, path: str) -> dict | None:
    """The answer for a GET under PREFIX, or None when the path is not one of this view's."""
    if path == PREFIX + "/strings":
        return strings()
    if path != PREFIX:
        return None
    mail = open_mail(routes)
    if mail is None:
        return {"ok": True, "available": False, "messages": []}
    try:
        return mail_log(mail)
    finally:
        mail.close()
