"""mail against a real Postgres, in a throwaway schema that is dropped afterwards. No network: SMTP is a fake.

Runs when TEST_DATABASE_URL is set, or with MAIL_TEST_USE_DEV_DB=1 (uses the database from .env / secrets/db.env,
only inside its own temporary schema). Synthetic data only."""

import dataclasses
import os
import uuid
from datetime import datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest
from psycopg import sql

from mail import contacts, db, drafts
from mail.config import load_settings
from mail.send import send_approved
from mail.store import ingest
from mail.tools import MailTools
from mail.transport import SendRejected, SendUnknown
from tracker.store import BlobStore

URL = os.environ.get("TEST_DATABASE_URL")
if not URL and os.environ.get("MAIL_TEST_USE_DEV_DB") == "1":
    from tracker.config import PROJECT_ROOT, database_url, load_env
    load_env(PROJECT_ROOT / ".env")
    load_env(PROJECT_ROOT / "secrets" / "db.env")
    URL = database_url()
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL (or MAIL_TEST_USE_DEV_DB=1) not set")

OWN = "desk@example.com"
SUP_ADDR = "anna@suppliers.example.net"
TEST_ADDR = "me@team.example.org"


class FakeSender:
    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    def send(self, msg):
        if self.fail:
            raise self.fail
        self.sent.append(msg)
        return "accepted"


@pytest.fixture
def env(tmp_path):
    schema = f"mail_test_{uuid.uuid4().hex[:8]}"
    base = load_settings(load_files=False)
    s = dataclasses.replace(base, database_url=URL, schema=schema, address=OWN, display_name="Desk",
                            send_mode="test_only", sender_registered=False, test_addresses=(TEST_ADDR,),
                            data_dir=tmp_path / "mail_data", tracker_data_dir=tmp_path / "data",
                            kb_schema=f"nokb_{uuid.uuid4().hex[:6]}", min_gap_per_recipient=0)
    conn = db.connect(URL, schema)
    db.migrate(conn, schema)
    conn.execute("SET search_path TO {}, public".format(schema))
    # Supplier contact via the profile fallback (no kb schema in this test).
    prof = tmp_path / "data" / "current" / "suppliers" / "SUP-0028_Test" / "profile.csv"
    prof.parent.mkdir(parents=True)
    prof.write_text("supplier_id;name;contact_name;contact_email;preferred_language\n"
                    f"SUP-0028;Test Karton;Anna;{SUP_ADDR};pl\n")
    counts = contacts.sync(conn, s)
    conn.commit()
    assert counts["origin"] == "profile" and counts["added"] == 2
    yield s, conn, BlobStore(s.data_dir)
    conn.rollback()
    conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    conn.commit()
    conn.close()


def make_draft(conn, s, to=None, supplier="SUP-0028", lang="pl"):
    return drafts.create_draft(conn, s, subject="Potwierdzenie ilości", lang=lang,
                               body_local="Dzień dobry, proszę potwierdzić ilość książek.",
                               body_en="Hello, please confirm the art book quantity.",
                               cause={"action_id": "A-1", "issue_key": "artbook"}, created_by="agent:chaser",
                               supplier_id=None if to else supplier, to=to)


def reply_to(raw_out: bytes, sender: str, body: str, attach: bytes | None = None, subject=None) -> bytes:
    import email
    orig = email.message_from_bytes(raw_out)
    outer = MIMEMultipart()
    outer["From"], outer["To"] = sender, OWN
    outer["Subject"] = subject or "Odp: " + orig["Subject"]
    outer["Message-ID"] = f"<{uuid.uuid4().hex}@suppliers.example.net>"
    outer["In-Reply-To"] = orig["Message-ID"]
    outer["References"] = orig["Message-ID"]
    outer.attach(MIMEText(body, "plain", "utf-8"))
    if attach:
        part = MIMEApplication(attach, "pdf")
        part.add_header("Content-Disposition", "attachment", filename="Oferta.pdf")
        outer.attach(part)
    return outer.as_bytes()


def test_contact_rules(env):
    s, conn, _ = env
    with pytest.raises(drafts.DraftError) as e:
        drafts.create_draft(conn, s, subject="x", lang="en", body_local="Hello", body_en="Hello",
                            cause={"a": 1}, created_by="agent", to=["stranger@evil.test"])
    assert e.value.code == "recipient_not_allowed"
    conn.rollback()
    with pytest.raises(drafts.DraftError) as e:
        make_draft(conn, s, lang="en")
    assert e.value.code == "wrong_language"
    conn.rollback()


def test_approval_rules_and_send_to_test_contact(env):
    s, conn, blobs = env
    d = make_draft(conn, s, to=[TEST_ADDR])
    conn.commit()
    fake = FakeSender()
    assert send_approved(conn, s, fake, d["draft_id"], str(uuid.uuid4()))["error"] == "approval_not_found"
    with pytest.raises(drafts.DraftError):
        drafts.approve(conn, d["draft_id"], "agent:planner")
    conn.rollback()
    rej = drafts.approve(conn, d["draft_id"], "Julian", decision="rejected")
    conn.commit()
    assert send_approved(conn, s, fake, d["draft_id"], rej["approval_id"])["error"] == "approval_not_approved"

    ok = drafts.approve(conn, d["draft_id"], "Julian", role="team", policy_ref="test")
    conn.commit()
    # Content changed after approval: the approval no longer covers it.
    conn.execute("UPDATE drafts SET body_local = body_local || ' Zmiana.', content_sha256 = 'x' WHERE draft_id = %s",
                 (d["draft_id"],))
    conn.commit()
    assert send_approved(conn, s, fake, d["draft_id"], ok["approval_id"])["error"] == "draft_hash_corrupt"
    # Edited properly (hash recomputed) after the click: the old approval does not cover the new text.
    row = drafts.load_draft(conn, d["draft_id"])
    new_hash = drafts.content_hash(row["to_addrs"], row["cc_addrs"], row["subject"], row["lang"], row["body_local"],
                                   row["body_en"])
    conn.execute("UPDATE drafts SET content_sha256 = %s WHERE draft_id = %s", (new_hash, d["draft_id"]))
    conn.commit()
    assert send_approved(conn, s, fake, d["draft_id"], ok["approval_id"])["error"] == "approval_hash_mismatch"
    assert fake.sent == []

    d2 = make_draft(conn, s, to=[TEST_ADDR])
    a2 = drafts.approve(conn, d2["draft_id"], "Julian")
    conn.commit()
    expired = send_approved(conn, s, fake, d2["draft_id"], a2["approval_id"],
                            now=datetime.now(timezone.utc) + timedelta(days=2))
    assert expired["error"] == "approval_expired"
    r = send_approved(conn, s, fake, d2["draft_id"], a2["approval_id"])
    assert r["status"] == "sent" and len(fake.sent) == 1
    msg = fake.sent[0]
    assert msg["Subject"].endswith(f"[{d2['tag']}]") and msg["Message-ID"] == r["message_id"]
    assert send_approved(conn, s, fake, d2["draft_id"], a2["approval_id"])["error"] == "draft_not_sendable"
    assert len(fake.sent) == 1

    # The test contact replies with an attachment and a rewritten subject; In-Reply-To links it.
    raw = reply_to(msg.as_bytes(), TEST_ADDR, "Potwierdzam 8000 sztuk.", b"%PDF-1.4 x", subject="Odp: nowy temat")
    res = ingest(conn, blobs, raw, OWN, s.tag_prefix)
    conn.commit()
    assert res.new and res.thread_id == d2["thread_id"] and res.match.method == "in_reply_to"
    assert ingest(conn, blobs, raw, OWN, s.tag_prefix).new is False      # idempotent
    conn.commit()
    att = conn.execute("SELECT sha256, kb_path FROM attachments").fetchone()
    assert blobs.has(att["sha256"]) and att["kb_path"].startswith("mail/TEST/")
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM events ORDER BY id")]
    assert "document_added" not in kinds        # test contacts never feed kb
    t = MailTools(s, conn, sender=fake)
    w = t.wait_for_reply(d2["thread_id"], timeout_s=1)
    assert w["status"] == "replied"
    assert "untrusted_content" in w["message"]["content"]


def test_modes_and_supplier_flow(env):
    s, conn, blobs = env
    fake = FakeSender()
    d = make_draft(conn, s)
    a = drafts.approve(conn, d["draft_id"], "Julian")
    conn.commit()
    # test_only must not reach a real supplier.
    assert send_approved(conn, s, fake, d["draft_id"], a["approval_id"])["error"] == "mode_test_only_blocked"
    live = dataclasses.replace(s, send_mode="live")
    assert send_approved(conn, live, fake, d["draft_id"], a["approval_id"])["error"] == "sender_not_registered"
    dry = dataclasses.replace(s, send_mode="dry_run")
    r = send_approved(conn, dry, None, d["draft_id"], a["approval_id"])
    assert r["status"] == "dry_run" and fake.sent == []
    # A dry run uses up the approval; going live needs a fresh click.
    live = dataclasses.replace(s, send_mode="live", sender_registered=True)
    assert send_approved(conn, live, fake, d["draft_id"], a["approval_id"])["error"] == "approval_used"
    a2 = drafts.approve(conn, d["draft_id"], "Julian")
    conn.commit()
    db.set_setting(conn, "sending_enabled", False)
    conn.commit()
    assert send_approved(conn, live, fake, d["draft_id"], a2["approval_id"])["error"] == "sending_paused"
    db.set_setting(conn, "sending_enabled", True)
    conn.commit()
    r = send_approved(conn, live, fake, d["draft_id"], a2["approval_id"])
    assert r["status"] == "sent" and len(fake.sent) == 1

    raw = reply_to(fake.sent[0].as_bytes(), SUP_ADDR, "Możemy wysłać 8 000 sztuk w niedzielę.", b"%PDF-1.4 y")
    res = ingest(conn, blobs, raw, OWN, s.tag_prefix)
    conn.commit()
    assert res.match.trust == "supplier_contact" and res.thread_id == d["thread_id"]
    paths = [r["payload"]["path"] for r in conn.execute("SELECT payload FROM events WHERE kind = 'document_added'")]
    assert any(p.endswith("/email_body.txt") for p in paths) and any(p.endswith("/Oferta.pdf") for p in paths)
    assert all(p.startswith("mail/SUP-0028/") for p in paths)
    status = conn.execute("SELECT status FROM threads WHERE thread_id = %s", (d["thread_id"],)).fetchone()["status"]
    assert status == "replied"


def test_rate_limit_and_smtp_outcomes(env):
    s, conn, _ = env
    s = dataclasses.replace(s, rate_global_hour=1)
    d1, d2, d3 = (make_draft(conn, s, to=[TEST_ADDR]) for _ in range(3))
    a1, a2, a3 = (drafts.approve(conn, d["draft_id"], "Julian") for d in (d1, d2, d3))
    conn.commit()
    r = send_approved(conn, s, FakeSender(fail=SendRejected("550 no")), d1["draft_id"], a1["approval_id"])
    assert r["status"] == "failed"                                   # failed sends do not count
    r = send_approved(conn, s, FakeSender(fail=SendUnknown("reset")), d2["draft_id"], a2["approval_id"])
    assert r["status"] == "unknown"
    assert send_approved(conn, s, FakeSender(), d3["draft_id"], a3["approval_id"])["error"] == "rate_limited"
    state = conn.execute("SELECT state FROM sends WHERE draft_id = %s", (d2["draft_id"],)).fetchone()["state"]
    assert state == "unknown"


def test_unknown_sender_not_threaded_and_not_for_kb(env):
    s, conn, blobs = env
    d = make_draft(conn, s, to=[TEST_ADDR])
    a = drafts.approve(conn, d["draft_id"], "Julian")
    conn.commit()
    fake = FakeSender()
    send_approved(conn, s, fake, d["draft_id"], a["approval_id"])
    raw = reply_to(fake.sent[0].as_bytes(), "billing@suppliers.example.net", "New bank details: ...")
    res = ingest(conn, blobs, raw, OWN, s.tag_prefix)
    conn.commit()
    assert res.thread_id is None and res.match.trust == "unknown_sender"
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM events ORDER BY id")]
    assert "unmatched_received" in kinds and "document_added" not in kinds
    assert MailTools(s, conn).list_unmatched()["messages"][0]["from_addr"] == "billing@suppliers.example.net"
    # The unknown sender's mail is not a reply in the thread, so waiting times out.
    import time
    start = time.monotonic()
    w = MailTools(s, conn).wait_for_reply(d["thread_id"], timeout_s=1.5)
    assert w["status"] == "timeout" and time.monotonic() - start >= 1.4
