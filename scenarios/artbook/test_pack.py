"""The scenario pack and ops/demo/reset.py. Database tests create and drop their own throwaway demo database."""

import importlib.util
import json
import os
import uuid
from datetime import timedelta
from email import message_from_bytes, policy
from pathlib import Path

import psycopg
import pytest
from psycopg import conninfo, sql

from kb import db as kb_db
from kb.mail_source import MailSource
from plan import clock, contracts, db as plan_db
from sourcing import promises
from tracker import web
from tracker.lookahead import Lookahead, append_records
from tracker.store import BlobStore

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("demo_reset", HERE.parents[1] / "ops" / "demo" / "reset.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

URL = os.environ.get("TEST_DATABASE_URL")
DEADLINE = "2031-03-09T20:00:00+01:00"
VALUES = {"unit_loss": "7", "budget": "300", "part": "printed booklet", "offer_quantity": "250", "offer_price": "2.5"}


@pytest.fixture(autouse=True)
def no_overrides(monkeypatch):
    for var in (clock.ENV_VAR, "PLAN_SCHEMA", "PGHOSTADDR", "PGSERVICE", demo.URL_ENV):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def pack():
    return demo.load_pack(HERE)


@pytest.fixture
def inputs(pack):
    return demo.initial_inputs(pack, VALUES)


@pytest.fixture
def demo_db():
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    if conninfo.conninfo_to_dict(URL).get("host") not in demo.LOOPBACK:
        pytest.skip("only a local throwaway Postgres")
    name = f"pack_{uuid.uuid4().hex[:8]}_demo"
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield conninfo.make_conninfo(URL, dbname=name)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.mark.parametrize("url", [
    "",
    "postgresql://u:p@db.example.org/launch_demo",
    "postgresql://u:p@127.0.0.1/tracker",
    "postgresql://u:p@127.0.0.1,db.example.org/launch_demo",
    "host=localhost hostaddr=192.0.2.4 dbname=launch_demo",
    "service=somewhere dbname=launch_demo",
    "dbname=launch_demo",
    "postgresql://127.0.0.1",
])
def test_anything_but_a_local_demo_database_is_refused(url):
    with pytest.raises(demo.DemoError):
        demo.check_target(url)


@pytest.mark.parametrize("var", ["PGHOSTADDR", "PGSERVICE"])
def test_environment_cannot_redirect_the_connection(monkeypatch, var):
    monkeypatch.setenv(var, "x")
    with pytest.raises(demo.DemoError, match=var):
        demo.check_target("postgresql://u:p@localhost/launch_demo")


def test_another_database_needs_the_flag_and_a_person(monkeypatch):
    url = "postgresql://u:p@db.example.org/launch"
    with pytest.raises(demo.DemoError, match="refusing"):
        demo.check_target(url, "other", ask=lambda _: "launch")
    monkeypatch.setattr(demo.sys.stdin, "isatty", lambda: False)
    with pytest.raises(demo.DemoError, match="person"):
        demo.check_target(url, "launch")
    with pytest.raises(demo.DemoError, match="not confirmed"):
        demo.check_target(url, "launch", ask=lambda _: "yes")
    assert demo.check_target(url, "launch", ask=lambda _: "launch") == "launch"


def test_refusal_comes_before_any_connection(pack, tmp_path, monkeypatch):
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: pytest.fail("connected"))
    url = "postgresql://u:p@db.example.org/launch"
    with pytest.raises(demo.DemoError, match="refusing"):
        demo.reset(url, pack, DEADLINE, VALUES, tmp_path / "log")
    with pytest.raises(demo.DemoError, match="refusing"):
        demo.replay(url, pack, "offer", tmp_path / "log", tmp_path)


def test_the_database_url_of_the_app_is_never_used(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/app_demo")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: pytest.fail("connected"))
    args = [str(HERE), "reset", "--log", str(tmp_path / "log"), "--deadline", DEADLINE]
    for key, value in VALUES.items():
        args += ["--set", f"{key}={value}"]
    assert demo.main(args) == 2
    assert demo.URL_ENV in capsys.readouterr().err


def test_only_the_plan_schema_can_be_dropped(pack, tmp_path, monkeypatch):
    monkeypatch.setenv("PLAN_SCHEMA", "kb")
    with pytest.raises(demo.DemoError, match="PLAN_SCHEMA"):
        demo.reset("postgresql://localhost/x_demo", pack, DEADLINE, VALUES, tmp_path / "log")


@pytest.mark.parametrize("key,value", [
    ("part", ""), ("part", "booklet\nBcc: x@example.org"), ("deposit_pct", "101"), ("offer_price", "nan"),
    ("offer_quantity", "many"), ("nonsense", "1"),
])
def test_bad_inputs_are_refused(pack, key, value):
    with pytest.raises(demo.DemoError):
        demo.initial_inputs(pack, {**VALUES, key: value})


def test_records_label_every_input_and_follow_the_clock_script(pack, inputs):
    due = clock.parse(DEADLINE)
    launch, event = demo.opening_records(pack, due, inputs)
    for record in (launch, event):
        contracts.validate_record(record)
    assert event["at"] == (due - timedelta(hours=26)).isoformat()
    assert launch["demo_times"] == [(due - timedelta(hours=h)).isoformat() for h in (22, 14)]
    assert launch["team_offline_at"] == (due - timedelta(hours=10)).isoformat()
    assert all(q["why"].startswith("Scenario input") for q in launch["goal_questions"])
    assert len(event["detail"]["reasoning"]) == len(pack["inputs"])
    assert "did not make this offer" in event["detail"]["happened"]


def test_inputs_edited_on_the_screen_reach_the_mail(pack, inputs, tmp_path):
    log = tmp_path / "log.jsonl"
    append_records(log, demo.opening_records(pack, clock.parse(DEADLINE), inputs))
    screen = Lookahead(log)
    # the route lives in tracker/web.py until the screen split moves it to tracker/lookahead_web.py
    route = getattr(web, "lookahead_goal", None) or importlib.import_module("tracker.lookahead_web").lookahead_goal
    route(screen, {"answers": {**inputs, "deposit_pct": "35"}})
    assert demo.current_inputs(pack, log)["deposit_pct"] == "35"
    assert b"35 percent" in demo.render(pack, "offer", demo.current_inputs(pack, log), "desk@example.org")


@pytest.mark.parametrize("beat", ["offer", "check_in", "cancellation"])
def test_every_mail_says_it_is_a_scenario(pack, inputs, beat):
    inputs["part"] = "Broschüre"
    msg = message_from_bytes(demo.render(pack, beat, inputs, "desk@example.org"), policy=policy.default)
    assert msg["Subject"].startswith("[Scenario]") and msg["X-Lookahead-Scenario"] == "scenario input"
    assert msg.get_content().startswith("[Scenario input")
    assert inputs["part"] in msg.get_content() and "${" not in msg.get_content()
    assert msg["From"] is None   # a teammate sends it from their own test inbox


def test_the_check_in_wording_is_a_promise_the_ledger_can_time(pack, inputs):
    assert "load within 4 h" in demo.render(pack, "check_in", inputs, "desk@example.org").decode()
    promise = promises.promise_from_statement(
        promise_id="p", supplier_id="supplier-a", what="loads", fallback="use the backup",
        made_at=DEADLINE, within=f"within {inputs['loads_within_h']} h")
    assert clock.parse(promise["next_check"]) - clock.parse(DEADLINE) == timedelta(hours=4)


def test_reset_rebuilds_plan_and_nothing_else(pack, demo_db, tmp_path):
    demo.reset(demo_db, pack, DEADLINE, VALUES, tmp_path / "one")
    with psycopg.connect(demo_db) as conn:
        conn.execute("INSERT INTO plan.goals (statement) VALUES ('old')")
        conn.execute("CREATE TABLE public.keep (x int)")
        conn.execute("INSERT INTO public.keep VALUES (1)")
    demo.reset(demo_db, pack, DEADLINE, VALUES, tmp_path / "two")
    with plan_db.connect(demo_db) as conn:
        assert conn.execute("SELECT count(*) AS n FROM goals").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM public.keep").fetchone()["n"] == 1
        assert clock.now(conn) == clock.parse(DEADLINE) - timedelta(hours=26)
        assert clock.read(conn)["rate"] == 0
    with pytest.raises(demo.DemoError, match="new log"):
        demo.reset(demo_db, pack, DEADLINE, VALUES, tmp_path / "two")


def test_an_outside_dependent_stops_the_reset(pack, demo_db, tmp_path):
    demo.reset(demo_db, pack, DEADLINE, VALUES, tmp_path / "one")
    with psycopg.connect(demo_db) as conn:
        conn.execute("CREATE VIEW public.watch AS SELECT statement FROM plan.goals")
    with pytest.raises(psycopg.errors.DependentObjectsStillExist):
        demo.reset(demo_db, pack, DEADLINE, VALUES, tmp_path / "two")
    assert not (tmp_path / "two").exists()
    with psycopg.connect(demo_db) as conn:
        conn.execute("SELECT * FROM public.watch")


def test_a_log_that_cannot_be_written_keeps_the_old_plan(pack, demo_db, tmp_path, monkeypatch):
    demo.reset(demo_db, pack, DEADLINE, VALUES, tmp_path / "one")
    with psycopg.connect(demo_db) as conn:
        conn.execute("INSERT INTO plan.goals (statement) VALUES ('old')")

    def full(*args):
        raise OSError("disk full")

    monkeypatch.setattr(demo, "append_records", full)
    with pytest.raises(OSError):
        demo.reset(demo_db, pack, DEADLINE, VALUES, tmp_path / "two")
    with psycopg.connect(demo_db) as conn:
        assert conn.execute("SELECT count(*) FROM plan.goals").fetchone()[0] == 1


def test_replay_reaches_kb_as_scenario_mail_and_sends_nothing(pack, demo_db, tmp_path):
    log, blobs = tmp_path / "log", tmp_path / "mail"
    demo.reset(demo_db, pack, DEADLINE, VALUES, log)
    for beat in pack["beats"]:
        assert demo.replay(demo_db, pack, beat, log, blobs).new
        assert not demo.replay(demo_db, pack, beat, log, blobs).new
    with psycopg.connect(demo_db) as conn:
        docs = [r[0] for r in conn.execute("SELECT payload FROM mail.events WHERE kind = 'document_added'")]
        assert conn.execute("SELECT count(*) FROM mail.sends").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM mail.drafts").fetchone()[0] == 0
    assert len(docs) == 3
    for doc in docs:
        assert doc["trust"] == "test" and doc["scenario"] is True and doc["persona_id"] == "supplier-a"
        assert b"Scenario input" in BlobStore(blobs).read(doc["sha256"])
    with kb_db.connect(demo_db, "kb") as conn:
        kb_db.migrate(conn, "kb")
        conn.commit()
        seen = MailSource("mail", blobs).catch_up(conn, tmp_path / "structured", {})
    assert len(seen) == 3 and all(d["status"] != "failed" for d in seen), json.dumps(seen)


@pytest.fixture
def second_db(demo_db):
    name = f"pack_{uuid.uuid4().hex[:8]}_demo"
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield conninfo.make_conninfo(URL, dbname=name)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_a_second_rehearsal_on_the_same_log_path_delivers_its_mail_again(pack, demo_db, second_db, tmp_path):
    log, blobs, sent = tmp_path / "log", tmp_path / "mail", []
    for url in (demo_db, second_db):
        log.unlink(missing_ok=True)
        demo.reset(url, pack, DEADLINE, VALUES, log)
        for beat in pack["beats"]:
            assert demo.replay(url, pack, beat, log, blobs).new
            assert not demo.replay(url, pack, beat, log, blobs).new
        with psycopg.connect(url) as conn:
            sent += [r[0] for r in conn.execute("SELECT message_id FROM mail.messages")]
    assert len(set(sent)) == 2 * len(pack["beats"])


@pytest.mark.parametrize("how", ["replayed", "from a test inbox"])
def test_a_database_with_the_last_rehearsals_mail_is_not_reset(pack, inputs, demo_db, tmp_path, how):
    demo.reset(demo_db, pack, DEADLINE, VALUES, tmp_path / "one")
    if how == "replayed":
        demo.replay(demo_db, pack, "cancellation", tmp_path / "one", tmp_path / "mail")
    else:
        raw = demo.render(pack, "cancellation", {**inputs, "part": "Broschüre"}, pack["persona"]["desk"],
                          sender="teammate@example.org", message_id="<inbox-1@example.org>")
        with demo.mail_db.connect(demo_db, demo.MAIL_SCHEMA) as conn:
            demo.mail_db.migrate(conn, demo.MAIL_SCHEMA)
            demo.mail_store.ingest(conn, BlobStore(tmp_path / "mail"), raw, pack["persona"]["desk"], demo.TAG_PREFIX)
            conn.commit()
    with psycopg.connect(demo_db) as conn:
        conn.execute("INSERT INTO plan.goals (statement) VALUES ('old')")
    with pytest.raises(demo.DemoError, match="fresh demo database"):
        demo.reset(demo_db, pack, DEADLINE, VALUES, tmp_path / "two")
    assert not (tmp_path / "two").exists()
    with psycopg.connect(demo_db) as conn:
        assert conn.execute("SELECT count(*) FROM plan.goals").fetchone()[0] == 1


@pytest.mark.parametrize("key,bad", [("deposit_pct", "500"), ("offer_quantity", "many"), ("part", "")])
def test_one_bad_edit_keeps_the_last_valid_value_and_says_so(pack, inputs, tmp_path, key, bad):
    log, good = tmp_path / "log.jsonl", {**inputs, "loads_within_h": "6"}
    append_records(log, demo.opening_records(pack, clock.parse(DEADLINE), inputs))
    append_records(log, [{"kind": "goal", "answers": good}, {"kind": "goal", "answers": {**good, key: bad}}])
    said = []
    values = demo.current_inputs(pack, log, said.append)
    assert values == good
    assert len(said) == 1 and key in said[0] and good[key] in said[0]
    for beat in pack["beats"]:
        assert demo.render(pack, beat, values, "desk@example.org")


def test_a_corrected_edit_clears_the_warning(pack, inputs, tmp_path):
    log = tmp_path / "log.jsonl"
    append_records(log, demo.opening_records(pack, clock.parse(DEADLINE), inputs))
    append_records(log, [{"kind": "goal", "answers": {**inputs, "deposit_pct": v}} for v in ("500", "35")])
    said = []
    assert demo.current_inputs(pack, log, said.append)["deposit_pct"] == "35"
    assert said == []


@pytest.mark.parametrize("typed", ["7 000", "7\u00a0000", "7\u202f000"])
def test_space_grouped_numbers_are_read(pack, typed):
    assert demo.initial_inputs(pack, {**VALUES, "offer_quantity": typed})["offer_quantity"] == "7000"


@pytest.mark.parametrize("typed", ["2.5", "1e3", "7,000", "1 00", "7 0000"])
def test_a_quantity_that_is_not_a_plain_whole_number_is_refused(pack, typed):
    with pytest.raises(demo.DemoError):
        demo.initial_inputs(pack, {**VALUES, "offer_quantity": typed})
