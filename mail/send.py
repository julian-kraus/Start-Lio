"""send_approved: the only path from a draft to SMTP. Every check that fails returns a reason and is audited."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import db
from .config import MailSettings
from .drafts import content_hash, load_draft, outbound_message_id, render_mime
from .parse import parse
from .transport import SendRejected, SendUnknown

SENDABLE = ("draft", "dry_run_sent", "failed")


def _blocked(conn, actor, draft_id, code, detail="") -> dict:
    db.audit(conn, actor, "send_blocked", "draft", draft_id, {"reason": code, "detail": detail})
    conn.commit()
    return {"ok": False, "status": "blocked", "error": code, "detail": detail, "draft_id": draft_id}


def _rate_limit(conn, s: MailSettings, supplier_id, recipients, now) -> str | None:
    live = "state IN ('sending', 'sent', 'unknown')"
    hour = now - timedelta(hours=1)
    total = conn.execute(f"SELECT count(*) AS n FROM sends WHERE {live} AND at > %s", (hour,)).fetchone()["n"]
    if total >= s.rate_global_hour:
        return f"global limit {s.rate_global_hour}/h reached"
    if supplier_id:
        n = conn.execute(f"SELECT count(*) AS n FROM sends WHERE {live} AND at > %s AND supplier_id = %s",
                         (hour, supplier_id)).fetchone()["n"]
        if n >= s.rate_per_supplier_hour:
            return f"limit {s.rate_per_supplier_hour}/h for {supplier_id} reached"
    gap = now - timedelta(seconds=s.min_gap_per_recipient)
    if conn.execute(f"SELECT 1 FROM sends WHERE {live} AND at > %s AND recipients && %s LIMIT 1",
                    (gap, list(recipients))).fetchone():
        return f"less than {int(s.min_gap_per_recipient)}s since the last mail to one of {list(recipients)}"
    return None


def send_approved(conn, s: MailSettings, sender, draft_id: str, approval_id: str, actor: str = "agent",
                  now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    conn.commit()   # start from a clean transaction so the row lock below is ours
    d = load_draft(conn, draft_id, lock=True)
    if not d:
        return _blocked(conn, actor, draft_id, "draft_not_found")
    if d["status"] not in SENDABLE:
        return _blocked(conn, actor, draft_id, "draft_not_sendable", d["status"])
    if content_hash(d["to_addrs"], d["cc_addrs"], d["subject"], d["lang"], d["body_local"], d["body_en"]) \
            != d["content_sha256"]:
        return _blocked(conn, actor, draft_id, "draft_hash_corrupt", "stored content does not match its hash")
    a = conn.execute("SELECT * FROM approvals WHERE approval_id = %s FOR UPDATE", (approval_id,)).fetchone()
    if not a:
        return _blocked(conn, actor, draft_id, "approval_not_found", approval_id)
    if a["decision"] != "approved":
        return _blocked(conn, actor, draft_id, "approval_not_approved", a["decision"])
    if str(a["draft_id"]) != str(draft_id):
        return _blocked(conn, actor, draft_id, "approval_draft_mismatch")
    if a["content_sha256"] != d["content_sha256"]:
        return _blocked(conn, actor, draft_id, "approval_hash_mismatch", "draft changed after approval")
    if a["used_at"] is not None:
        return _blocked(conn, actor, draft_id, "approval_used", str(a["used_at"]))
    if now - a["decided_at"] > timedelta(seconds=s.approval_ttl):
        return _blocked(conn, actor, draft_id, "approval_expired", str(a["decided_at"]))
    if a["approver"].lower().startswith(("agent", "mail:", "system")):
        return _blocked(conn, actor, draft_id, "approver_not_human", a["approver"])

    recipients = list(d["to_addrs"]) + list(d["cc_addrs"])
    rows = conn.execute("SELECT address, is_test FROM contacts WHERE address = ANY(%s) AND revoked_at IS NULL "
                        "AND send_allowed", (recipients,)).fetchall()
    allowed = {r["address"]: r["is_test"] for r in rows}
    missing = [r for r in recipients if r not in allowed]
    if missing:
        return _blocked(conn, actor, draft_id, "recipient_not_allowed", str(missing))

    mode = s.send_mode
    if mode == "test_only" and not all(allowed.values()):
        return _blocked(conn, actor, draft_id, "mode_test_only_blocked", "a recipient is not a test contact")
    if mode == "live" and not s.sender_registered:
        return _blocked(conn, actor, draft_id, "sender_not_registered", "set MAIL_SENDER_REGISTERED=yes")
    if mode != "dry_run":
        if db.get_setting(conn, "sending_enabled", True) is not True:
            return _blocked(conn, actor, draft_id, "sending_paused", "python -m mail resume")
        if (why := _rate_limit(conn, s, d["supplier_id"], recipients, now)):
            return _blocked(conn, actor, draft_id, "rate_limited", why)

    msg = render_mime(conn, d, s)
    raw = msg.as_bytes()
    mid = outbound_message_id(str(draft_id), s)
    conn.execute("UPDATE approvals SET used_at = %s WHERE approval_id = %s", (now, approval_id))

    if mode == "dry_run":
        conn.execute("INSERT INTO sends (draft_id, approval_id, mode, state, recipients, supplier_id, message_id, "
                     "rendered, at) VALUES (%s,%s,'dry_run','dry_run',%s,%s,%s,%s,%s)",
                     (draft_id, approval_id, recipients, d["supplier_id"], mid, raw, now))
        conn.execute("UPDATE drafts SET status = 'dry_run_sent', updated_at = now() WHERE draft_id = %s", (draft_id,))
        db.audit(conn, actor, "send_dry_run", "draft", draft_id, {"approval_id": approval_id, "to": recipients})
        db.emit(conn, "send_completed", {"draft_id": draft_id, "thread_id": str(d["thread_id"]), "mode": mode,
                                         "state": "dry_run", "message_id": mid})
        conn.commit()
        return {"ok": True, "status": "dry_run", "mode": mode, "draft_id": draft_id, "message_id": mid,
                "thread_id": str(d["thread_id"]), "rendered_preview": raw.decode("utf-8", "replace")[:4000]}

    send_id = conn.execute(
        "INSERT INTO sends (draft_id, approval_id, mode, state, recipients, supplier_id, message_id, rendered, at) "
        "VALUES (%s,%s,%s,'sending',%s,%s,%s,%s,%s) RETURNING send_id",
        (draft_id, approval_id, mode, recipients, d["supplier_id"], mid, raw, now)).fetchone()["send_id"]
    db.audit(conn, actor, "send_started", "draft", draft_id, {"approval_id": approval_id, "mode": mode,
                                                               "to": recipients, "send_id": send_id})
    conn.commit()   # the 'sending' row exists before the first byte goes out: no double send on a crash

    try:
        response = sender.send(msg)
    except SendRejected as exc:
        conn.execute("UPDATE sends SET state = 'failed', error = %s, updated_at = now() WHERE send_id = %s",
                     (str(exc)[:1000], send_id))
        conn.execute("UPDATE drafts SET status = 'failed', updated_at = now() WHERE draft_id = %s", (draft_id,))
        db.audit(conn, actor, "send_failed", "draft", draft_id, {"send_id": send_id, "error": str(exc)[:300]})
        conn.commit()
        return {"ok": False, "status": "failed", "error": "smtp_rejected", "detail": str(exc)[:300],
                "draft_id": draft_id}
    except SendUnknown as exc:
        conn.execute("UPDATE sends SET state = 'unknown', error = %s, updated_at = now() WHERE send_id = %s",
                     (str(exc)[:1000], send_id))
        db.audit(conn, actor, "send_unknown", "draft", draft_id, {"send_id": send_id, "error": str(exc)[:300]})
        conn.commit()
        return {"ok": False, "status": "unknown", "error": "smtp_unknown", "detail": "not retried; the poller "
                "checks the Sent folder", "draft_id": draft_id}

    _record_sent(conn, s, d, send_id, mid, raw, response, now)
    db.audit(conn, actor, "send_completed", "draft", draft_id, {"send_id": send_id, "mode": mode})
    conn.commit()
    return {"ok": True, "status": "sent", "mode": mode, "draft_id": draft_id, "message_id": mid,
            "thread_id": str(d["thread_id"])}


def _record_sent(conn, s, d, send_id, mid, raw, response, now) -> None:
    p = parse(raw)
    conn.execute("UPDATE sends SET state = 'sent', smtp_response = %s, updated_at = now() WHERE send_id = %s",
                 (response, send_id))
    conn.execute("UPDATE drafts SET status = 'sent', updated_at = now() WHERE draft_id = %s", (d["draft_id"],))
    conn.execute(
        "INSERT INTO messages (message_id, thread_id, direction, kind, match_method, from_addr, to_addrs, cc_addrs, "
        "subject, in_reply_to, refs, date_header, received_at, supplier_id, trust, lang, body_text, body_en, raw, "
        "raw_sha256) VALUES (%s,%s,'out','outbound','sent',%s,%s,%s,%s,%s,%s,%s,%s,%s,'self',%s,%s,%s,%s,%s) "
        "ON CONFLICT (message_id) DO NOTHING",
        (mid, d["thread_id"], s.address, d["to_addrs"], d["cc_addrs"], d["subject"], p.in_reply_to, p.references,
         p.date, now, d["supplier_id"], d["lang"], d["body_local"], d["body_en"], raw, p.raw_sha256))
    conn.execute("UPDATE threads SET status = 'waiting_reply', last_message_at = %s WHERE thread_id = %s",
                 (now, d["thread_id"]))
    db.emit(conn, "send_completed", {"draft_id": str(d["draft_id"]), "thread_id": str(d["thread_id"]),
                                     "mode": s.send_mode, "state": "sent", "message_id": mid,
                                     "supplier_id": d["supplier_id"]})


def resolve_unknown(conn, fetcher, settle_after: timedelta = timedelta(minutes=10)) -> int:
    """Sends whose SMTP outcome is unknown: found in All Mail -> sent, still missing after a while -> failed."""
    rows = conn.execute("SELECT s.*, d.thread_id FROM sends s JOIN drafts d USING (draft_id) "
                        "WHERE s.state = 'unknown'").fetchall()
    if not rows:
        return 0
    folders = fetcher.folders()
    fetcher.select(folders.get("\\All", "INBOX"))
    resolved = 0
    for r in rows:
        if fetcher.find_message_id(r["message_id"]):
            conn.execute("UPDATE sends SET state = 'sent', updated_at = now() WHERE send_id = %s", (r["send_id"],))
            conn.execute("UPDATE drafts SET status = 'sent', updated_at = now() WHERE draft_id = %s", (r["draft_id"],))
            conn.execute("UPDATE threads SET status = 'waiting_reply' WHERE thread_id = %s", (r["thread_id"],))
            db.audit(conn, "mail:worker", "send_resolved", "draft", str(r["draft_id"]), {"state": "sent"})
            resolved += 1
        elif datetime.now(timezone.utc) - r["at"] > settle_after:
            conn.execute("UPDATE sends SET state = 'failed', error = COALESCE(error,'') || ' | not found in All Mail', "
                         "updated_at = now() WHERE send_id = %s", (r["send_id"],))
            conn.execute("UPDATE drafts SET status = 'failed', updated_at = now() WHERE draft_id = %s",
                         (r["draft_id"],))
            db.audit(conn, "mail:worker", "send_resolved", "draft", str(r["draft_id"]), {"state": "failed"})
            resolved += 1
    return resolved
