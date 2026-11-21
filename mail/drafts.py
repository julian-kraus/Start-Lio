"""Drafts: validated, hashed and stored before anyone can approve or send them."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from email import policy as email_policy
from email.message import EmailMessage
from email.utils import formataddr, formatdate

from psycopg.types.json import Jsonb

from . import db
from .config import MailSettings
from .parse import detect_lang
from .store import create_thread

LANGS = ("ko", "zh", "pl", "en")
MAX_SUBJECT = 200
MAX_BODY = 20_000


class DraftError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def render_subject(subject: str, tag: str, prefix: str) -> str:
    clean = re.sub(r"\s*\[%s-[0-9A-Z]{4}\]" % re.escape(prefix), "", subject, flags=re.I).strip()
    return f"{clean} [{tag}]"


def content_hash(to, cc, subject, lang, body_local, body_en) -> str:
    canonical = json.dumps({"to": sorted(to), "cc": sorted(cc), "subject": subject, "lang": lang,
                            "body_local": body_local, "body_en": body_en}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def outbound_message_id(draft_id: str, s: MailSettings) -> str:
    return f"<gh6.{draft_id}@{s.domain}>"


def _active_contacts(conn, addrs):
    rows = conn.execute("SELECT address, supplier_id, is_test, language FROM contacts WHERE address = ANY(%s) "
                        "AND revoked_at IS NULL AND send_allowed", (list(addrs),)).fetchall()
    return {r["address"]: r for r in rows}


def create_draft(conn, s: MailSettings, *, subject: str, lang: str, body_local: str, body_en: str,
                 cause: dict, created_by: str, supplier_id: str | None = None, to: list[str] | None = None,
                 cc: list[str] | None = None, po_numbers: list[str] | None = None, thread_id: str | None = None,
                 reply_to_message_id: str | None = None, lang_override_reason: str | None = None,
                 body_back_en: str | None = None) -> dict:
    to = [a.strip().lower() for a in (to or []) if a.strip()]
    cc = [a.strip().lower() for a in (cc or []) if a.strip()]
    po_numbers = [str(p).strip() for p in (po_numbers or []) if str(p).strip()]
    if not subject or not subject.strip() or len(subject) > MAX_SUBJECT or "\n" in subject or "\r" in subject:
        raise DraftError("bad_subject", f"subject must be one line, 1..{MAX_SUBJECT} chars")
    if lang not in LANGS:
        raise DraftError("bad_lang", f"lang must be one of {LANGS}")
    for name, body in (("body_local", body_local), ("body_en", body_en)):
        if not body or not body.strip() or len(body) > MAX_BODY:
            raise DraftError("bad_body", f"{name} must be non-empty and at most {MAX_BODY} chars")
    if not isinstance(cause, dict) or not cause:
        raise DraftError("missing_cause", "cause must name what triggered this email, e.g. {action_id, issue_key}")

    if not to:
        if not supplier_id:
            raise DraftError("no_recipient", "give supplier_id or to")
        rows = conn.execute("SELECT address FROM contacts WHERE supplier_id = %s AND revoked_at IS NULL "
                            "AND send_allowed AND NOT is_test ORDER BY address", (supplier_id,)).fetchall()
        if not rows:
            raise DraftError("no_contact", f"no active contact for {supplier_id}")
        to = [rows[0]["address"]]
    allowed = _active_contacts(conn, to + cc)
    blocked = [a for a in to + cc if a not in allowed]
    if blocked:
        raise DraftError("recipient_not_allowed", f"not in the allowlist: {blocked}")
    for a in to + cc:
        c = allowed[a]
        if c["is_test"]:
            continue
        if supplier_id is None:
            supplier_id = c["supplier_id"]
        if c["supplier_id"] != supplier_id:
            raise DraftError("mixed_suppliers", f"{a} belongs to {c['supplier_id']}, not {supplier_id}")

    warnings = []
    expected = next((allowed[a]["language"] for a in to if not allowed[a]["is_test"] and allowed[a]["language"]),
                    None)
    if expected and lang != expected and not lang_override_reason:
        raise DraftError("wrong_language", f"supplier language is {expected!r}; set lang_override_reason to deviate")
    detected = detect_lang(body_local)
    if detected and detected != lang:
        warnings.append(f"body_local looks like {detected!r}, declared {lang!r}")
    if lang == "en" and body_local.strip() != body_en.strip():
        warnings.append("lang is en but body_en differs from body_local; the approver sees body_en")

    if reply_to_message_id:
        row = conn.execute("SELECT thread_id::text AS t FROM messages WHERE message_id = %s",
                           (reply_to_message_id,)).fetchone()
        if not row or not row["t"]:
            raise DraftError("unknown_message", f"no threaded message {reply_to_message_id}")
        if thread_id and thread_id != row["t"]:
            raise DraftError("thread_mismatch", "reply_to_message_id belongs to another thread")
        thread_id = row["t"]
    if thread_id:
        t = conn.execute("SELECT thread_id::text AS thread_id, tag, supplier_id FROM threads WHERE thread_id = %s",
                         (thread_id,)).fetchone()
        if not t:
            raise DraftError("unknown_thread", thread_id)
        if t["supplier_id"] and supplier_id and t["supplier_id"] != supplier_id:
            raise DraftError("thread_mismatch", f"thread belongs to {t['supplier_id']}")
        tag = t["tag"]
    else:
        t = create_thread(conn, s.tag_prefix, supplier_id, subject.strip(), po_numbers, cause)
        thread_id, tag = t["thread_id"], t["tag"]

    subject_rendered = render_subject(subject.strip(), tag, s.tag_prefix)
    digest = content_hash(to, cc, subject_rendered, lang, body_local, body_en)
    draft_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO drafts (draft_id, thread_id, reply_to_message_id, supplier_id, to_addrs, cc_addrs, subject, lang, "
        "body_local, body_en, body_back_en, po_numbers, cause, content_sha256, created_by) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (draft_id, thread_id, reply_to_message_id, supplier_id, to, cc, subject_rendered, lang, body_local, body_en,
         body_back_en, po_numbers, Jsonb({**cause, **({"lang_override_reason": lang_override_reason}
                                                       if lang_override_reason else {})}), digest, created_by))
    db.audit(conn, created_by, "draft_created", "draft", draft_id,
             {"thread_id": thread_id, "supplier_id": supplier_id, "to": to, "cc": cc, "content_sha256": digest,
              "warnings": warnings})
    return {"ok": True, "draft_id": draft_id, "thread_id": thread_id, "tag": tag, "content_sha256": digest,
            "to": to, "cc": cc, "subject_rendered": subject_rendered, "warnings": warnings}


def load_draft(conn, draft_id: str, lock: bool = False) -> dict | None:
    return conn.execute("SELECT * FROM drafts WHERE draft_id = %s" + (" FOR UPDATE" if lock else ""),
                        (draft_id,)).fetchone()


# RFC 5322 allows 998 chars per line. The default (78) makes Python fold long Message-IDs in In-Reply-To and
# References into RFC 2047 encoded words, which breaks threading on the supplier's side.
MIME_POLICY = email_policy.default.clone(max_line_length=998)


def new_message() -> EmailMessage:
    return EmailMessage(policy=MIME_POLICY)


def render_mime(conn, draft: dict, s: MailSettings) -> EmailMessage:
    msg = new_message()
    msg["From"] = formataddr((s.display_name, s.address)) if s.display_name else s.address
    msg["To"] = ", ".join(draft["to_addrs"])
    if draft["cc_addrs"]:
        msg["Cc"] = ", ".join(draft["cc_addrs"])
    msg["Subject"] = draft["subject"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = outbound_message_id(str(draft["draft_id"]), s)
    if draft["reply_to_message_id"]:
        parent = conn.execute("SELECT message_id, refs FROM messages WHERE message_id = %s",
                              (draft["reply_to_message_id"],)).fetchone()
        if parent:
            msg["In-Reply-To"] = parent["message_id"]
            msg["References"] = " ".join(list(parent["refs"] or [])[-10:] + [parent["message_id"]])
    else:
        last = conn.execute("SELECT message_id FROM messages WHERE thread_id = %s ORDER BY received_at DESC, id DESC "
                            "LIMIT 1", (draft["thread_id"],)).fetchone()
        if last:
            msg["In-Reply-To"] = last["message_id"]
            msg["References"] = last["message_id"]
    msg.set_content(draft["body_local"], charset="utf-8")
    return msg


def approve(conn, draft_id: str, approver: str, decision: str = "approved", role: str | None = None,
            policy_ref: str | None = None) -> dict:
    """Record a human decision on the exact current content of a draft. The UI calls this on the click."""
    if decision not in ("approved", "rejected"):
        raise DraftError("bad_decision", decision)
    if not approver or approver.lower().startswith(("agent", "mail:", "system")):
        raise DraftError("approver_not_human", "approvals must name a person")
    d = load_draft(conn, draft_id)
    if not d:
        raise DraftError("draft_not_found", draft_id)
    approval_id = str(uuid.uuid4())
    conn.execute("INSERT INTO approvals (approval_id, draft_id, content_sha256, decision, approver, approver_role, "
                 "policy_ref) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                 (approval_id, draft_id, d["content_sha256"], decision, approver, role, policy_ref))
    db.audit(conn, approver, f"draft_{decision}", "draft", draft_id,
             {"approval_id": approval_id, "content_sha256": d["content_sha256"], "role": role, "policy": policy_ref})
    return {"ok": True, "approval_id": approval_id, "decision": decision, "content_sha256": d["content_sha256"]}
