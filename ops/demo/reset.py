#!/usr/bin/env python3
"""Prepare and drive a scenario pack on a demo database. Nothing here sends email.

  reset.py PACK reset   --log NEW.jsonl --deadline ISO --set id=value ...   rebuild plan, start clock and log
               [--history-supplier ID]      score the persona on that supplier's history in kb
  reset.py PACK render  BEAT --log LOG [--to ADDRESS]                       print the mail a teammate sends
  reset.py PACK replay  BEAT --log LOG --mail-data DIR                      feed that mail to the demo mail store

The database is named with --database-url or LOOKAHEAD_DEMO_DATABASE_URL. DATABASE_URL and .env are never read.
A demo database is on loopback and its name ends in `_demo`. `reset` refuses any other database before connecting,
unless a person passes --live-db <name> and types that name at a terminal. `replay` has no such override.
Each rehearsal needs a fresh database: `reset` refuses one that already holds scenario mail, because that mail
and the facts read from it outlive the plan schema and would break the next rehearsal's promises.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import uuid
from datetime import timedelta
from email import message_from_string, policy
from email.utils import format_datetime, formataddr
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from psycopg import conninfo, sql  # noqa: E402

from mail import contacts, db as mail_db, store as mail_store  # noqa: E402
from plan import clock, contracts, db as plan_db  # noqa: E402
from tracker.lookahead import append_records  # noqa: E402
from tracker.store import BlobStore  # noqa: E402

URL_ENV = "LOOKAHEAD_DEMO_DATABASE_URL"
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
DEMO_SUFFIX = "_demo"
PRODUCER = "agent:scenario"
MAIL_SCHEMA = "mail"
KB_SCHEMA = "kb"
TAG_PREFIX = "DEMO"
SUBJECT_MARK = "[Scenario]"
NUMBER = re.compile(r"(?:[0-9]+|[0-9]{1,3}(?:[ \u00a0\u202f][0-9]{3})+)(?:\.[0-9]+)?")


class DemoError(ValueError):
    pass


def load_pack(folder: Path) -> dict:
    pack = json.loads((folder / "scenario.json").read_text(encoding="utf-8"))
    return {**pack, "folder": folder}


def personas(pack: dict) -> list[dict]:
    """Supplier A first, then the other scenario personas (a backup supplier)."""
    return [pack["persona"], *pack.get("personas", {}).values()]


def persona_by_id(pack: dict, persona_id: str) -> dict:
    for persona in personas(pack):
        if persona["persona_id"] == persona_id:
            return persona
    raise DemoError(f"unknown persona {persona_id!r}: one of {', '.join(p['persona_id'] for p in personas(pack))}")


def beat_mail(pack: dict, beat: str) -> tuple[str, dict]:
    """A beat is a mail file sent by Supplier A, or {"mail": file, "persona": id} for another persona."""
    if beat not in pack["beats"]:
        raise DemoError(f"unknown beat {beat!r}: one of {', '.join(pack['beats'])}")
    spec = pack["beats"][beat]
    if isinstance(spec, str):
        return spec, pack["persona"]
    return spec["mail"], persona_by_id(pack, spec["persona"])


# ---- which database may be touched -------------------------------------------

def check_target(url: str, live_db: str | None = None, ask=None) -> str:
    """The database name, or DemoError. Decided from the URL alone, before anything connects."""
    if not url:
        raise DemoError(f"name the demo database with --database-url or {URL_ENV}")
    for var in ("PGHOSTADDR", "PGSERVICE"):
        if os.environ.get(var):
            raise DemoError(f"{var} is set and could redirect the connection: unset it")
    try:
        parts = conninfo.conninfo_to_dict(url)
    except psycopg.Error:
        raise DemoError("the database URL cannot be parsed") from None  # its text may hold a password
    host, name = str(parts.get("host") or ""), str(parts.get("dbname") or "")
    if not host or not name or parts.get("service"):
        raise DemoError("the URL must name a host and a database, not a service")
    local = host in LOOPBACK and parts.get("hostaddr", "127.0.0.1") in LOOPBACK
    if local and name.endswith(DEMO_SUFFIX):
        return name
    if live_db != name:
        raise DemoError(f"refusing {name!r}: not a loopback database ending in {DEMO_SUFFIX}")
    if ask is None:
        if not sys.stdin.isatty():
            raise DemoError("--live-db needs a person at a terminal")
        ask = input
    if ask(f"This deletes the plan schema of {name} on {host}. Type the database name: ").strip() != name:
        raise DemoError("not confirmed")
    return name


def refuse_used(conn, pack: dict) -> None:
    table = f"{MAIL_SCHEMA}.messages"
    if not conn.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (table,)).fetchone()["ok"]:
        return
    if conn.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM {} WHERE from_addr = ANY(%s) OR strpos(subject, %s) > 0) AS used")
                    .format(sql.Identifier(MAIL_SCHEMA, "messages")),
                    ([p["address"] for p in personas(pack)], SUBJECT_MARK)).fetchone()["used"]:
        raise DemoError("this database holds the mail of an earlier rehearsal: create a fresh demo database")


def drop_plan(conn, schema: str) -> None:
    """Drop the schema's own tables without CASCADE: anything outside that depends on them stops the reset."""
    tables = [r["relname"] for r in conn.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind IN ('r', 'p') ORDER BY 1", (schema,))]
    if tables:
        conn.execute(sql.SQL("DROP TABLE {}").format(
            sql.SQL(", ").join(sql.Identifier(schema, t) for t in tables)))
    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {}").format(sql.Identifier(schema)))


# ---- inputs ------------------------------------------------------------------

def checked(spec: dict, value) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        raise DemoError(f"scenario input {spec['id']} has no value: pass --set {spec['id']}=...")
    if "unit" in spec:
        if not NUMBER.fullmatch(text):
            raise DemoError(f"scenario input {spec['id']} must be a number")
        text = re.sub(r"\s", "", text)   # space-grouped, as people type it
        if spec.get("whole") and "." in text:
            raise DemoError(f"scenario input {spec['id']} must be a whole number")
        number = float(text)
        if not math.isfinite(number) or not spec.get("min", -math.inf) <= number <= spec.get("max", math.inf):
            raise DemoError(f"scenario input {spec['id']} is out of range")
    if not text.isprintable():   # values end up in mail headers
        raise DemoError(f"scenario input {spec['id']} must be one printable line")
    return text


def initial_inputs(pack: dict, overrides: dict) -> dict:
    unknown = set(overrides) - {s["id"] for s in pack["inputs"]}
    if unknown:
        raise DemoError(f"unknown scenario inputs: {sorted(unknown)}")
    return {s["id"]: checked(s, overrides.get(s["id"], s.get("suggested"))) for s in pack["inputs"]}


def warn_operator(text: str) -> None:
    print(f"WARNING: {text}", file=sys.stderr)


def current_inputs(pack: dict, log: Path, warn=warn_operator) -> dict:
    """What the launch record suggested, replaced by the last valid answers given on the screen.

    The screen saves any text, so an invalid edit is reported and the value before it stays: no beat is blocked."""
    if not log.is_file():
        raise DemoError(f"no record log at {log}: run reset first")
    launch, answers = None, []
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("kind") == "launch":
            launch, answers = record, []
        elif record.get("kind") == "goal" and record.get("producer") in (None, "app"):
            if isinstance(record.get("answers"), dict):
                answers.append(record["answers"])
    if not launch or not launch.get("scenario"):
        raise DemoError("the log holds no scenario launch: run reset first")
    values = {**launch.get("scenario_inputs", {}),
              **{q["id"]: q.get("suggested") for q in launch.get("goal_questions", [])}}
    result = {}
    for spec in pack["inputs"]:
        key = spec["id"]
        value, problem = checked(spec, values.get(key)), None
        for given in answers:
            if key in given:
                try:
                    value, problem = checked(spec, given[key]), None
                except DemoError as exc:
                    problem = str(exc)
        if problem:
            warn(f"{problem}. The edit of {key} on the screen is ignored, using {value}")
        result[key] = value
    return result


def opening_records(pack: dict, deadline, inputs: dict) -> list[dict]:
    script, cast = pack["clock"], personas(pack)

    def before(hours) -> str:
        return (deadline - timedelta(hours=hours)).isoformat()

    start = before(script["start_h_before_deadline"])
    questions = [{"id": s["id"], "q": s["q"], "suggested": inputs[s["id"]],
                  "why": s.get("why", "Scenario input. You can change it here."),
                  **({"unit": s["unit"]} if "unit" in s else {})} for s in pack["inputs"] if s.get("on_screen") or not any(q.get("on_screen") for q in pack["inputs"])]
    launch = {"kind": "launch", "name": pack["name"], "mode": "live", "deadline": deadline.isoformat(),
              "scenario": True, "rehearsal_id": uuid.uuid4().hex,  # unique per reset: mail outlives the plan schema
              "team_offline_at": before(script["offline_h_before_deadline"]),
              "demo_times": [before(h) for h in script["stops_h_before_deadline"]], "goal_questions": questions,
              "scenario_inputs": inputs, **({"edition": pack["edition"]} if pack.get("edition") else {})}
    labelled = {"kind": "event", "type": "scenario", "severity": "info", "at": start,
                "title": pack.get("label") or f"Scenario: {' and '.join(p['display_name'] for p in cast)} "
                         f"{'is' if len(cast) == 1 else 'are'} played by the team",
                "source": {"scenario_input": pack["name"]},
                "detail": {"happened": " ".join(p["note"] for p in cast),
                           "reasoning": [{"text": f"Scenario input: {q['q']}",
                                          "value": " ".join(filter(None, (str(q["suggested"]), q.get("unit"))))}
                                         for q in questions]}}
    records = [{"producer": PRODUCER, **r} for r in (launch, labelled)]   # the live screen hides unnamed producers
    for record in records:
        contracts.validate_record(record)
    return records


def history_card(conn, pack: dict, supplier_id: str, at: str, persona: dict | None = None) -> dict:
    """The persona's supplier card: the reliability score of a real supplier's history, read from kb now.

    The pack names no supplier and holds no score. The card keeps the persona's id, which its promises carry,
    and says on its face whose history it is and that only the history is real."""
    from sourcing import reliability

    def read(table: str) -> list[dict]:
        if not conn.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"{KB_SCHEMA}.{table}",)).fetchone()["ok"]:
            raise DemoError("this database has no kb facts: load them before binding a supplier history")
        return conn.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(KB_SCHEMA, table))).fetchall()

    card = reliability.card(reliability.history(read("current_facts"), read("believed")), supplier_id)
    if not card["score"]["history"]["n"]:
        raise DemoError(f"kb holds no delivery history of supplier {supplier_id!r}")
    persona, whose = persona or pack["persona"], card.get("name") or supplier_id
    record = {**card, "producer": PRODUCER, "supplier_id": persona["persona_id"], "name": persona["display_name"],
              "at": at, "history_supplier_id": supplier_id, "flags": ["Replayed offer", f"Real history: {whose}"],
              "persona": f"{persona['note']} The score is computed from the history of {whose} in kb facts."}
    contracts.validate_record(record)
    return record


# ---- commands ----------------------------------------------------------------

def reset(url: str, pack: dict, deadline: str, overrides: dict, log: Path,
          live_db: str | None = None, ask=None, history_supplier: str | None = None,
          persona_history: dict | None = None) -> list[dict]:
    schema = plan_db.schema_name()
    if schema != plan_db.DEFAULT_SCHEMA:
        raise DemoError(f"PLAN_SCHEMA is {schema!r}: reset only ever drops {plan_db.DEFAULT_SCHEMA!r}")
    due = clock.parse(deadline)
    records = opening_records(pack, due, initial_inputs(pack, overrides))
    if log.exists() and log.stat().st_size:
        raise DemoError(f"{log} already has records: give each rehearsal a new log")
    bound = {persona_id: (persona_by_id(pack, persona_id), supplier) for persona_id, supplier in (persona_history or {}).items()}
    check_target(url, live_db, ask)
    with plan_db.connect(url, schema) as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (plan_db.MIGRATION_LOCK,))
            refuse_used(conn, pack)
            if history_supplier:
                records.append(history_card(conn, pack, history_supplier, records[1]["at"]))
            for persona in personas(pack)[1:]:
                if persona["persona_id"] not in bound:     # named on screen, with no history claimed
                    records.append({"kind": "supplier", "producer": PRODUCER, "supplier_id": persona["persona_id"],
                                    "name": persona["display_name"], "flags": ["Simulated"],
                                    "persona": persona["note"]})
            for persona, supplier in bound.values():
                records.append(history_card(conn, pack, supplier, records[1]["at"], persona))
            drop_plan(conn, schema)
            plan_db.migrate(conn, schema)
            clock.set_scenario(conn, records[1]["at"], rate=0, by="demo reset")
            # inside the transaction: a log that cannot be written leaves the old plan in place
            append_records(log, records)
        conn.commit()
    return records


def render(pack: dict, beat: str, inputs: dict, to: str, sender: str | None = None, at=None,
           message_id: str | None = None) -> bytes:
    mail, persona = beat_mail(pack, beat)
    text = Template((pack["folder"] / mail).read_text(encoding="utf-8")).substitute(
        inputs, persona=persona["display_name"])
    head, _, body = text.partition("\n\n")
    msg = message_from_string(head + "\n\n", policy=policy.SMTP)
    msg.set_content(body)   # set from the text, so non-ASCII inputs get a valid transfer encoding
    msg["To"] = to
    if sender:
        msg["From"] = formataddr((persona["display_name"], sender))
    if at:
        msg["Date"] = format_datetime(at)
    if message_id:
        msg["Message-ID"] = message_id
    return msg.as_bytes()


def replay(url: str, pack: dict, beat: str, log: Path, mail_data: Path):
    """The mail path of a real inbox, minus IMAP: mail.store.ingest emits the events kb consumes."""
    check_target(url)
    inputs = current_inputs(pack, log)
    persona, desk = beat_mail(pack, beat)[1], pack["persona"]["desk"]
    with plan_db.connect(url) as plan_conn:
        if not clock.is_scenario(plan_conn):
            raise DemoError("no scenario clock is running: run reset first")
        at = clock.now(plan_conn)
    # one message per beat and rehearsal, so a second key press adds nothing
    key = hashlib.sha256(f"{log.resolve()}|{log.read_text(encoding='utf-8').splitlines()[0]}|{beat}".encode())
    raw = render(pack, beat, inputs, desk, persona["address"], at,
                 f"<{key.hexdigest()[:32]}@scenario.invalid>")
    with mail_db.connect(url, MAIL_SCHEMA) as conn:
        mail_db.migrate(conn, MAIL_SCHEMA)
        conn.commit()
        contacts.add_manual(conn, persona["address"], None, True, "demo replay")
        contacts.set_persona(conn, persona["address"], persona["persona_id"], "demo replay")
        result = mail_store.ingest(conn, BlobStore(mail_data), raw, desk, TAG_PREFIX, now=at)
        conn.commit()
    return result


def assignment(text: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError("expected id=value")
    return key, value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pack", type=Path)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("reset", "render", "replay"):
        p = sub.add_parser(name)
        p.add_argument("--log", type=Path, required=True)
        if name != "render":
            p.add_argument("--database-url", default=os.environ.get(URL_ENV, ""))
        if name != "reset":
            p.add_argument("beat")
        if name == "reset":
            p.add_argument("--deadline", required=True, help="ISO time with offset")
            p.add_argument("--set", type=assignment, action="append", default=[])
            p.add_argument("--history-supplier", help="kb supplier id whose real history scores the persona")
            p.add_argument("--persona-history", type=assignment, action="append", default=[],
                           help="persona_id=kb supplier id, e.g. the backup supplier's real history")
            p.add_argument("--live-db", help="for a person: the exact name of a database that is not a demo one")
        elif name == "render":
            p.add_argument("--to")
        else:
            p.add_argument("--mail-data", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        pack = load_pack(args.pack)
        if args.cmd == "reset":
            launch = reset(args.database_url, pack, args.deadline, dict(args.set), args.log, args.live_db,
                           history_supplier=args.history_supplier,
                           persona_history=dict(args.persona_history))[0]
            print(f"plan rebuilt, clock frozen at the start, jumps to {', '.join(launch['demo_times'])}")
        elif args.cmd == "render":
            mail = render(pack, args.beat, current_inputs(pack, args.log), args.to or pack["persona"]["desk"])
            sys.stdout.write(mail.decode())
        else:
            result = replay(args.database_url, pack, args.beat, args.log, args.mail_data)
            print(f"{args.beat}: {'stored as scenario mail' if result.new else 'already replayed'}")
    except (DemoError, clock.ClockError) as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
