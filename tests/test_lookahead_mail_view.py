"""Email log view of /lookahead/v2: the read-only endpoint over MailTools and the view script. Synthetic data only."""

import dataclasses
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from psycopg import sql

from tracker import lookahead_api_mail as api
from tracker.lookahead import LookaheadError

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "tracker" / "lookahead_static"
URL = os.environ.get("TEST_DATABASE_URL")
WRITE_METHODS = ("draft_email", "cancel_draft", "send_approved", "wait_for_reply", "get_draft", "poll_events")


class FakeTools:
    """Answers like MailTools; records every call. Write methods fail the test if reached."""

    def __init__(self, threads=(), unmatched=(), contacts=()):
        self.calls, self.threads, self.unmatched, self.contacts = [], list(threads), list(unmatched), list(contacts)
        self.conn = None

    def list_contacts(self, supplier_id=None):
        self.calls.append("list_contacts")
        return {"ok": True, "contacts": self.contacts}

    def list_threads(self, limit=20, **kw):
        self.calls.append("list_threads")
        return {"ok": True, "threads": [t["thread"] for t in self.threads]}

    def get_thread(self, thread_id, max_chars_per_message=4000):
        self.calls.append("get_thread")
        found = [t for t in self.threads if t["thread"]["thread_id"] == thread_id]
        return {"ok": True, **found[0]} if found else {"ok": False, "error": "thread_not_found"}

    def list_unmatched(self, since=None, limit=50):
        self.calls.append("list_unmatched")
        return {"ok": True, "messages": self.unmatched}

    def __getattr__(self, name):
        if name in WRITE_METHODS:
            raise AssertionError(f"write method reached: {name}")
        raise AttributeError(name)


def wrapped(text):
    return {"untrusted_content": {"notice": "n", "truncated": False,
                                  "text": f"<<<EMAIL_CONTENT id=ab12>>>\n{text}\n<<<END_EMAIL_CONTENT id=ab12>>>"}}


def synthetic():
    out = {"message_id": "<m1@x>", "direction": "out", "from_addr": "desk@example.com", "to_addrs": ["anna@sup.example"],
           "subject": "Quote request", "date_header": "2030-01-01T09:00:00+00:00", "received_at": "2030-01-01T09:00:01+00:00",
           "lang": "en", "content": {"own_text": "Hello,\nplease quote.", "body_en": None}, "attachments": []}
    inb = {"message_id": "<m2@x>", "direction": "in", "from_addr": "anna@sup.example", "to_addrs": ["desk@example.com"],
           "subject": "Re: Quote request", "date_header": None, "received_at": "2030-01-01T10:00:00+00:00",
           "lang": "pl", "content": wrapped("Dzien dobry"), "body_en": "Good day",
           "attachments": [{"filename": "quote.pdf"}]}
    thread = {"thread": {"thread_id": "t1", "supplier_id": "S-X"}, "drafts": [{"draft_id": "d-1"}], "messages": [out, inb]}
    stray = {"message_id": "<m3@x>", "from_addr": "who@else.example", "from_name": "Someone", "subject": "Hi",
             "received_at": "2030-01-01T08:00:00+00:00"}
    contacts = [{"address": "Anna@sup.example", "supplier_id": "S-X", "display_name": "Anna Example"}]
    return FakeTools([thread], [stray], contacts)


def test_log_lists_every_message_newest_first_with_thread_drafts_and_contact():
    tools = synthetic()
    got = api.mail_log(api.ReadOnlyMail(tools))
    assert got["available"] is True
    ids = [m["message_id"] for m in got["messages"]]
    assert ids == ["<m2@x>", "<m1@x>", "<m3@x>"]
    inb, out, stray = got["messages"]
    assert out["direction"] == "out" and out["body"] == "Hello,\nplease quote." and out["to"] == ["anna@sup.example"]
    assert out["contact"]["name"] == "Anna Example" and out["supplier_id"] == "S-X"
    assert inb["direction"] == "in" and inb["body"] == "Dzien dobry" and inb["body_en"] == "Good day"
    assert inb["attachments"] == ["quote.pdf"] and inb["draft_ids"] == ["d-1"] and inb["at"].startswith("2030-01-01T10")
    assert stray["thread_id"] is None and stray["body"] is None and stray["from_name"] == "Someone"
    assert set(tools.calls) <= set(api.READ_METHODS)


def test_read_only_mail_exposes_no_write_method():
    ro = api.ReadOnlyMail(synthetic())
    for name in WRITE_METHODS + ("approve", "sender", "conn"):
        assert not hasattr(ro, name), name


def test_no_send_draft_or_approve_path_in_the_endpoint_or_view():
    words = re.compile(r"draft_email|cancel_draft|send_approved|approve|\bsend\(|drafts\.|POST", re.I)
    code = (ROOT / "tracker" / "lookahead_api_mail.py").read_text().split('"""', 2)[2]
    js = (STATIC / "mail_view.js").read_text()
    assert not words.search(code.replace("ReadOnlyMail", ""))
    assert not re.search(r"method:\s*[\"']POST|approve|/accept|/reject", js)


def test_handle_answers_only_its_paths_and_shows_empty_state_without_a_store():
    class NoStore:
        def mail(self, actor):
            raise LookaheadError(503, "no database is configured")

    class Routes:
        decision_stores = NoStore()

    assert api.handle(Routes(), "/api/lookahead/other") is None
    assert api.handle(Routes(), api.PREFIX) == {"ok": True, "available": False, "messages": []}
    labels = api.handle(Routes(), api.PREFIX + "/strings")
    assert set(labels) == {"en", "zh"} and set(labels["en"]) == set(labels["zh"])


def test_failed_mail_read_is_an_error_not_an_empty_log():
    tools = synthetic()
    tools.list_contacts = lambda supplier_id=None: {"ok": False, "error": "internal_error"}
    with pytest.raises(LookaheadError):
        api.mail_log(api.ReadOnlyMail(tools))


# ---------------------------------------------------------------- view script, in node

PAGE = """
const els = {};
const el = () => ({dataset: {}, style: {}, innerHTML: "", textContent: "", querySelectorAll: () => [],
  classList: {add() {}, remove() {}, toggle() {}}, insertAdjacentHTML() {}, addEventListener() {}});
const fetched = [];
const answers = JSON.parse(process.argv[3]);
const ctx = {console, URLSearchParams, location: {hash: "", search: answers.search || ""}, history: {replaceState() {}},
  addEventListener() {}, requestAnimationFrame() {}, setTimeout, clearTimeout,
  fetch: (u, o) => { fetched.push([u, (o || {}).method || "GET"]);
    if (!u.startsWith("/api/lookahead/mail")) return new Promise(() => {});   // the screen's own state: set below
    return Promise.resolve({ok: true, json: () => Promise.resolve(u.endsWith("/strings") ? answers.strings : answers.log)}); },
  EventSource: class { addEventListener() {} },
  document: {documentElement: {lang: "en"}, querySelector: s => els[s] = els[s] || el(), querySelectorAll: () => [],
    getElementById: s => els["#" + s] = els["#" + s] || el()}};
ctx.window = ctx;
const vm = require("vm"), fs = require("fs");
vm.createContext(ctx);
for (const f of ["gp_app.js", "gp_plan.js", "mail_view.js"]) vm.runInContext(fs.readFileSync(process.argv[1] + "/" + f, "utf8"), ctx);
vm.runInContext("S = " + process.argv[2] + ";", ctx);
setTimeout(() => {
  vm.runInContext("MAIL.filter = " + JSON.stringify(answers.filter || "all") + "; mails();", ctx);
  console.log(JSON.stringify({list: els["#mList"].innerHTML, read: els["#mRead"].innerHTML, fetched}));
}, 50);
"""


def view(log, state=None, filter_="all", search=""):
    strings = json.loads((STATIC / "strings" / "mail.json").read_text())
    base = {"launch": None, "suppliers": [{"supplier_id": "S-X", "name": "Example Works"}], "decisions": []}
    out = subprocess.run(["node", "-e", PAGE, str(STATIC), json.dumps({**base, **(state or {})}),
                          json.dumps({"strings": strings, "log": log, "filter": filter_, "search": search})],
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node runs the screen scripts")


@needs_node
def test_view_lists_rows_and_shows_the_selected_message():
    log = api.mail_log(api.ReadOnlyMail(synthetic()))
    got = view(log, {"decisions": [{"decision_id": "dec-9", "draft_ids": ["d-1"], "supplier_id": "S-X"}]})
    assert got["list"].count('class="mitem') == 3 and 'class="mitem on" data-m="&lt;m2@x&gt;"' in got["list"]
    assert "Anna Example · Example Works" in got["list"] and 'class="mdir in"' in got["list"]
    assert "Received" in got["read"] and "Re: Quote request" in got["read"] and "Good day" in got["read"]
    assert "<dt>From</dt>" in got["read"] and "<dt>To</dt>" in got["read"] and "<dt>Time</dt>" in got["read"]
    assert 'data-mdec="dec-9"' in got["read"] and "See related decision" in got["read"] and "quote.pdf" in got["read"]
    assert all(method == "GET" for _, method in got["fetched"])


@needs_node
def test_view_filters_sent_and_received_and_has_an_empty_state():
    log = api.mail_log(api.ReadOnlyMail(synthetic()))
    sent = view(log, filter_="out")
    assert sent["list"].count('class="mitem') == 1 and "Sent by agent" in sent["read"]
    assert "See related decision" not in sent["read"]   # no decision on the records
    got = view(log, filter_="in")
    assert got["list"].count('class="mitem') == 2
    empty = view({"ok": True, "available": True, "messages": []})
    assert "No emails." in empty["list"] and empty["read"] == ""
    off = view({"ok": True, "available": False, "messages": []})
    assert "not connected" in off["list"]


@needs_node
def test_view_speaks_chinese_with_lang_zh():
    log = api.mail_log(api.ReadOnlyMail(synthetic()))
    got = view(log, search="?lang=zh")
    assert "收到" in got["read"] and "寄件人" in got["read"] and "Dzien dobry" in got["read"]


# ---------------------------------------------------------------- real MailTools on a throwaway schema


@pytest.fixture
def mail_stores(tmp_path):
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    from mail import db as mail_db
    from mail.config import load_settings
    from tracker import lookahead_decisions as decisions

    schema = f"mail_v5_{uuid.uuid4().hex[:8]}"
    settings = dataclasses.replace(load_settings(load_files=False), database_url=URL, schema=schema,
                                   address="desk@example.com", send_mode="dry_run", data_dir=tmp_path / "mail_data")
    conn = mail_db.connect(URL, schema, autocommit=True)
    mail_db.migrate(conn, schema)
    yield decisions.Stores(settings), conn
    conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    conn.close()


def counts(conn):
    return {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
            for t in ("drafts", "approvals", "sends", "audit_log", "messages", "threads")}


def test_endpoint_reads_the_mail_store_through_mailtools_and_writes_nothing(mail_stores):
    stores, conn = mail_stores
    tid = uuid.uuid4()
    conn.execute("INSERT INTO contacts (address, supplier_id, display_name, is_test, send_allowed, source) "
                 "VALUES ('anna@sup.example', 'S-X', 'Anna Example', false, true, 'test')")
    conn.execute("INSERT INTO threads (thread_id, tag, supplier_id, subject, status, last_message_at) "
                 "VALUES (%s, 'tag1', 'S-X', 'Quote request', 'open', '2030-01-01T10:00:00Z')", (tid,))
    for mid, direction, kind, frm, to, at, body in (
            ("<a@x>", "out", "outbound", "desk@example.com", ["anna@sup.example"], "2030-01-01T09:00:00Z", "Please quote."),
            ("<b@x>", "in", "reply", "anna@sup.example", ["desk@example.com"], "2030-01-01T10:00:00Z", "Here it is.")):
        conn.execute("INSERT INTO messages (message_id, thread_id, direction, kind, from_addr, to_addrs, subject, "
                     "received_at, supplier_id, trust, body_text, raw, raw_sha256) VALUES (%s, %s, %s, %s, %s, %s, "
                     "'Quote request', %s, 'S-X', %s, %s, '\\x00', 'x')",
                     (mid, tid, direction, kind, frm, to, at, "self" if direction == "out" else "supplier_contact", body))
    before = counts(conn)

    class Routes:
        decision_stores = stores

    got = api.handle(Routes(), api.PREFIX)
    assert [(m["direction"], m["body"]) for m in got["messages"]] == [("in", "Here it is."), ("out", "Please quote.")]
    assert got["messages"][0]["contact"]["name"] == "Anna Example" and got["messages"][0]["thread_id"] == str(tid)
    assert counts(conn) == before
