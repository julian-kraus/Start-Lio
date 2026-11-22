"""Test contacts playing a supplier persona: their mail feeds kb, labelled trust=test and scenario=true.

Same throwaway-schema setup as test_mail_integration.py. Synthetic data only."""

import uuid
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

from mail import contacts
from mail.__main__ import main as mail_main
from mail.store import BODY_FILENAME, ingest
from mail.tools import MailTools

from test_mail_integration import OWN, SUP_ADDR, TEST_ADDR, URL, env  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL (or MAIL_TEST_USE_DEV_DB=1) not set")

PERSONA = "PERSONA-7"


def inbound(sender: str, body: str, attach: bytes | None = None) -> bytes:
    msg = MIMEMultipart()
    msg["From"], msg["To"], msg["Subject"] = sender, OWN, "Quote for the order"
    msg["Message-ID"] = f"<{uuid.uuid4().hex}@team.example.org>"
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if attach:
        part = MIMEApplication(attach, "pdf")
        part.add_header("Content-Disposition", "attachment", filename="Quote.pdf")
        msg.attach(part)
    return msg.as_bytes()


def events(conn, kind):
    return [r["payload"] for r in conn.execute("SELECT payload FROM events WHERE kind = %s ORDER BY id", (kind,))]


def test_set_persona_rules(env):  # noqa: F811
    s, conn, _ = env
    contacts.set_persona(conn, TEST_ADDR, PERSONA, "test")
    assert contacts.persona_of(conn, TEST_ADDR) == PERSONA
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'contact_persona_set'"
                        ).fetchone()["n"] == 1
    with pytest.raises(ValueError, match="only test contacts"):
        contacts.set_persona(conn, SUP_ADDR, PERSONA, "test")
    with pytest.raises(ValueError, match="not a persona id"):
        contacts.set_persona(conn, TEST_ADDR, "bad id; drop", "test")
    with pytest.raises(ValueError, match="no active contact"):
        contacts.set_persona(conn, "nobody@example.org", PERSONA, "test")
    assert contacts.persona_of(conn, SUP_ADDR) is None
    # A later contact sync keeps the persona: it is set by a human, not by master data.
    contacts.sync(conn, s)
    assert contacts.persona_of(conn, TEST_ADDR) == PERSONA
    contacts.set_persona(conn, TEST_ADDR, None, "test")
    assert contacts.persona_of(conn, TEST_ADDR) is None


def test_persona_mail_emits_labelled_documents(env):  # noqa: F811
    s, conn, blobs = env
    contacts.set_persona(conn, TEST_ADDR, PERSONA, "test")
    conn.commit()
    res = ingest(conn, blobs, inbound(TEST_ADDR, "We can deliver 500 units.", b"%PDF-1.4 q"), OWN, s.tag_prefix)
    conn.commit()
    assert res.new and res.match.trust == "test"
    received = events(conn, "message_received")
    assert received[0]["trust"] == "test" and received[0]["persona_id"] == PERSONA and received[0]["scenario"]
    docs = events(conn, "document_added")
    assert len(docs) == 2
    for d in docs:
        assert d["trust"] == "test" and d["persona_id"] == PERSONA and d["scenario"] is True
        assert d["supplier_id"] is None
        assert d["path"].startswith(f"mail/TEST/{PERSONA}/")
    body = next(d for d in docs if d["path"].endswith(BODY_FILENAME))
    text = blobs.read(body["sha256"]).decode("utf-8")
    assert "Sender-Trust: test" in text and f"Persona-ID: {PERSONA}" in text and "Scenario:" in text
    # Agents read the message through MailTools with the test trust label.
    view = MailTools(s, conn).get_thread(res.thread_id)
    assert view["ok"] and view["messages"] and all(m["trust"] == "test" for m in view["messages"])


def test_test_contact_without_persona_and_real_contact_unchanged(env):  # noqa: F811
    s, conn, blobs = env
    ingest(conn, blobs, inbound(TEST_ADDR, "Just testing."), OWN, s.tag_prefix)
    conn.commit()
    assert events(conn, "document_added") == []
    assert events(conn, "message_received")[0]["persona_id"] is None
    ingest(conn, blobs, inbound(SUP_ADDR, "Offer attached.", b"%PDF-1.4 r"), OWN, s.tag_prefix)
    conn.commit()
    docs = events(conn, "document_added")
    assert len(docs) == 2
    for d in docs:
        assert d["trust"] == "supplier_contact" and d["supplier_id"] == "SUP-0028"
        assert "persona_id" not in d and "scenario" not in d
        assert d["path"].startswith("mail/SUP-0028/")


def test_cli_set_persona(env, monkeypatch, capsys):  # noqa: F811
    s, conn, _ = env
    monkeypatch.setattr("mail.__main__.load_settings", lambda: s)
    mail_main(["set-persona", TEST_ADDR, PERSONA])
    assert f"persona {PERSONA} set" in capsys.readouterr().out
    assert contacts.persona_of(conn, TEST_ADDR) == PERSONA
    mail_main(["set-persona", TEST_ADDR, "--clear"])
    assert contacts.persona_of(conn, TEST_ADDR) is None
