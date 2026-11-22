"""The monitor: a broken promise becomes a re-plan and one decision card on the Lookahead screen.

    python -m plan run [--log FILE] [--no-model]

It wakes on kb facts, kb issues and mail, and looks again every second in case a wake-up was lost. The ledger
(agents.broker) settles promises from supplier statements. For each newly broken promise the monitor scores the
supplier again, reruns the hedger on the open offers of the other suppliers and appends the records the screen
folds. It sends nothing and approves nothing.

Where the inputs are incomplete it fails closed: the card says what is missing and carries no action, so it
cannot be signed. A promise is marked as handled only after its records are in the log, so a crash repeats the
work under the same decision id instead of losing it.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Callable

import psycopg
from psycopg.types.json import Json

from kb import db as kb_db, events as kb_events
from mail import db as mail_db
from plan import clock, contracts, db as plan_db
from sourcing import coverage, facts as kb_facts, reliability, timing, weakspots
from sourcing.hedge import Input
from tracker.lookahead import append_records, fold
from tracker.lookahead_records import LookaheadError, validate
from tracker.lookahead_view import shown_live

from . import broker, hedger, llm

log = logging.getLogger("agents.monitor")

LOCK = 7_222_001
PRODUCER = "monitor"
HANDLED = "promise_broken"
BROKERED = "statement_brokered"
COMMITMENTS = ("confirm",)      # statements that make a promise worth watching (sourcing.promises.OUTCOMES settles)
BACKOFF = (1, 2, 5, 10, 30)
NO_SITE = "delivery site not on file"    # routed as a site, so a required release cannot be skipped


def _plain(value):
    return json.loads(json.dumps(value, default=hedger._json_value))


def screen_state(path: Path) -> dict:
    """What the live screen shows now. A bad or half-written line is not shown there and not read here."""
    shown = []
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for number, line in enumerate(lines, 1):
        try:
            record = validate(json.loads(line))
        except (ValueError, LookaheadError):
            continue
        if shown_live(record):
            shown.append({**record, "id": number})
    return fold(shown)


def current_goal(conn, state: dict) -> dict:
    """The stored goal, else the answers given on the screen. Nothing suggested counts as an answer."""
    row = conn.execute("SELECT * FROM goals WHERE superseded_at IS NULL ORDER BY created_at DESC LIMIT 1").fetchone()
    goal = {k: v for k, v in (row or state["goal"] or {}).items() if v is not None}
    if not goal.get("statement") or goal.get("unit_loss_eur") is None:
        raise ValueError("no goal with a statement and a loss per missing copy is set")
    return _plain(goal)


def part_of(conn, promise: dict) -> str:
    if promise.get("part_id"):
        return str(promise["part_id"])
    found = conn.execute("SELECT DISTINCT part_id FROM offers WHERE supplier_id = %s",
                         (promise["supplier_id"],)).fetchall()
    if len(found) != 1:
        raise ValueError("the promise names no part and the supplier's offers do not give exactly one")
    return found[0]["part_id"]


def units_per_copy(solved: dict, part: str) -> Input:
    """The largest use per copy among the editions, so a backup never claims more copies than it protects."""
    open_copies = {e["product_id"]: e["open"] for e in solved["editions"]}
    uses = []
    for row in next((p["allocation"] for p in solved["parts"] if str(p["material_id"]) == part), []):
        need, copies = row["need"], open_copies.get(row["product_id"])
        if need["value"] and copies and copies["value"]:
            refs = [{"fact_id": i} for i in dict.fromkeys([*need["fact_ids"], *copies["fact_ids"]])]
            uses.append((need["value"] / copies["value"], refs))
    if not uses:
        raise ValueError("the coverage has no use per copy of this part")
    value, refs = max(uses, key=lambda use: use[0])
    return Input(value, refs, "need of the part / open copies", computed=True)


def category_of(rows: list[dict], part: str) -> str | None:
    found = {str(r["value"]) for r in rows if r["fact_type"] == "component" and r["attribute"] == "category"
             and part in map(str, (r.get("subject") or {}).values())}
    return found.pop() if len(found) == 1 else None


def site_of(rows: list[dict]) -> str:
    found = {str(r["value"]) for r in rows
             if r["fact_type"] == "packout" and r["attribute"] == "site_id" and r["value"]}
    return found.pop() if len(found) == 1 else NO_SITE


def card_signer(approval: dict) -> dict:
    """Who signs, for the card. A name only when that person alone is enough: the accept button signs with it."""
    asked = [*approval["signers"], *approval["also_required"]]
    signer = dict(approval["signer"])
    if approval["status"] != "resolved" or len(asked) != 1:
        signer.pop("name", None)
    for key in ("role", "rule"):
        parts = [signer.get(key), *(s.get(key) for s in approval["also_required"])]
        if said := list(dict.fromkeys(part for part in parts if part)):
            signer[key] = ("; " if key == "rule" else " and ").join(said)
    return signer


def open_backups(conn, part: str, deadline, now, per_copy: Input, category: str | None,
                 site: str) -> tuple[list[hedger.Backup], list[str]]:
    """Open offers of suppliers that have not broken a promise on this part."""
    rows = conn.execute(
        "SELECT * FROM offers o WHERE part_id = %s AND status = 'open' AND NOT EXISTS (SELECT 1 FROM promises p "
        "WHERE p.state = 'broken' AND p.supplier_id = o.supplier_id AND (p.part_id IS NULL OR p.part_id = o.part_id)) "
        "ORDER BY offer_id", (part,)).fetchall()
    found, problems = [], []
    for row in rows:
        refs = [ref if isinstance(ref, dict) else {"fact_id": ref} for ref in row["sources"]]
        hours = row["lead_time_h"]
        if hours is None and row["available_at"]:
            hours = max(0.0, (row["available_at"] - now).total_seconds() / 3600)
        if not refs or hours is None:
            problems.append(f"{row['offer_id']}: the offer needs a source and a lead time")
            continue
        included = {"label": "included in the offer's lead time", "inputs": refs}
        try:
            chain = timing.Chain(load=timing.Leg("offer lead time", float(hours), refs[0]),
                                 **{name: timing.Leg(name, 0, included)
                                    for name in ("transit", "last_mile", "receiving")})
        except timing.TimingError as exc:
            problems.append(f"{row['offer_id']}: {exc}")
            continue
        found.append(hedger.Backup(
            {**row, "sources": refs}, chain, deadline, {"label": "launch deadline", "value": deadline.isoformat()},
            units_per_copy=per_copy, category=category, delivery_site=site,
            # plan.offers holds no deposit terms, so the whole order counts as committed
            cost_model=Input("full_order", refs, "no cancellation terms on the offer: whole order committed")))
    return found, problems


class Monitor:
    def __init__(self, plan_conn, kb_conn, client: llm.LLMClient, log_path: Path,
                 load_facts: Callable = kb_facts.load, kb_schema: str = "kb", mail=None):
        self.plan, self.kb, self.client, self.log = plan_conn, kb_conn, client, Path(log_path)
        self.load_facts, self.kb_schema, self.mail = load_facts, kb_schema, mail

    def tick(self) -> list[str]:
        """Settle what the suppliers said, then publish each break that is not handled. Returns decision ids."""
        statements = broker.read_statements(self.kb)
        self.kb.commit()
        self._broker_new(statements)
        for update in broker.apply_ledger(self.plan, statements):
            log.info("promise %s is %s", update["promise_id"], update["state"])
        self.plan.commit()
        broken = self.plan.execute(
            "SELECT * FROM promises p WHERE state = 'broken' AND NOT EXISTS (SELECT 1 FROM timeline_events e "
            "WHERE e.type = %s AND e.payload->>'promise_id' = p.promise_id) ORDER BY broken_at, promise_id",
            (HANDLED,)).fetchall()
        self.plan.commit()
        return [self._publish(promise) for promise in broken]

    def _broker_new(self, statements) -> None:
        """Hand each new commitment (a `confirm` statement) to the broker once, so a later cancellation has a
        registered promise to break. Done statements are marked in timeline_events, so a restart does not repeat."""
        done = {r["f"] for r in self.plan.execute(
            "SELECT payload->>'fact_id' AS f FROM timeline_events WHERE type = %s UNION "
            "SELECT src->>'fact_id' FROM promises, jsonb_array_elements(sources) src", (BROKERED,))}
        for s in statements:     # a statement an existing promise already cites is watched already
            if s.kind not in COMMITMENTS or done & {str(f) for f in s.all_fact_ids()}:
                continue
            said = {k: v for k, v in vars(s).items() if v not in (None, "", (), [])}
            try:
                _, registered = broker.run_broker(
                    self.client, self.plan, self.mail, now=s.at or clock.now(self.plan), statements=[s],
                    prompt="A supplier statement arrived. Register the commitment it makes, citing its fact id.\n"
                           + json.dumps(_plain(said), default=str))
                log.info("brokered statement %s: %d promise(s)", s.fact_id, len(registered))
            except (llm.AgentStop, ValueError) as exc:
                log.warning("broker could not register statement %s (%s)", s.fact_id, exc)
            self.plan.execute("INSERT INTO timeline_events (at, type, title, severity, supplier_id, payload, scenario) "
                              "VALUES (%s, %s, %s, 'info', %s, %s, %s)",
                              (s.at or clock.now(self.plan), BROKERED, f"Watching what {s.supplier_id} said",
                               s.supplier_id, Json({"fact_id": str(s.fact_id)}), clock.is_scenario(self.plan)))
            self.plan.commit()

    def _replan(self, promise: dict, part: str, state: dict, card: dict, rows: list[dict], now) -> tuple:
        """(recommendation or None, why there is none, category, delivery site)."""
        goal = current_goal(self.plan, state)
        said = goal.get("deadline") or (state["launch"] or {}).get("deadline")
        if not said:
            raise ValueError("no deadline is set")
        deadline = clock.parse(str(said))
        known = self.load_facts(self.kb, self.kb_schema)
        solved = coverage.compute(known, now=now, deadline=deadline)
        spots = weakspots.rank(known, solved, goal)
        spot = next((s for s in spots if str(s["part_id"]) == part), None)
        if not spot:
            raise ValueError("the part is not in the plan")
        if spot["detail"]["status"] != "critical":
            return None, f"the part is {spot['detail']['status']}, not critical: {spot['reason']}", None, None
        category, site = category_of(rows, part), site_of(rows)
        backups, problems = open_backups(self.plan, part, deadline, now, units_per_copy(solved, part), category, site)
        out = hedger.hedge_parts(self.plan, self.client, spots, backups, primaries={part: card}, goal=goal,
                                 approval_facts=rows, now=now)
        for rec in out["recommendations"]:
            if rec["part_id"] == part:
                return rec, None, category, site
        skipped = [why for s in out["skipped"] if s["part_id"] == part for why in s["reasons"]]
        return None, "; ".join(problems + skipped), category, site

    def _publish(self, promise: dict) -> str:
        now, state = clock.now(self.plan), screen_state(self.log)
        key, supplier = promise["promise_id"], promise["supplier_id"]
        decision_id = f"promise-broken:{key}:{promise['broken_at'].isoformat()}"
        part, rec, why, category, site, card = promise.get("part_id"), None, None, None, None, None
        try:
            part = part_of(self.plan, promise)
            rows = self.kb.execute("SELECT * FROM current_facts").fetchall()
            card = state["suppliers"].get(supplier)
            if "starting_point" not in ((card or {}).get("score") or {}):
                believed = self.kb.execute("SELECT * FROM believed").fetchall()
                card = reliability.card(reliability.history(rows, believed), supplier)
            card = reliability.update(card, "broken", key, promise["sources"], now.isoformat())
            rec, why, category, site = self._replan(promise, part, state, card, rows, now)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            raise
        except Exception as exc:    # the break must reach a person also when the re-plan cannot be computed
            log.exception("re-plan of promise %s failed", key)
            self.plan.rollback()
            why = f"{type(exc).__name__}: {exc}"
        finally:
            if not self.kb.closed:
                self.kb.rollback()

        records = self._records(promise, part, state, card, rec, why, category, site, decision_id, now)
        for record in records:
            contracts.validate_record(record)
        append_records(self.log, records, producer=PRODUCER)
        lane = promise.get("lane") or part
        before = [d["decision_id"] for d in records if d.get("status") == "superseded"]
        after = {"decision_id": decision_id, "recommendation_id": (rec or {}).get("recommendation_id"),
                 "kind": (rec or {}).get("kind"), "no_replan": why}
        self.plan.execute(
            "INSERT INTO timeline_events (at, type, title, severity, lane, supplier_id, payload, scenario) "
            "VALUES (%s, %s, %s, 'critical', %s, %s, %s, %s)",
            (promise["broken_at"], HANDLED, f"{supplier} broke its promise: {promise['what']}", lane, supplier,
             Json(_plain({"promise_id": key, "diff": {"before": before, "after": after},
                          "sources": promise["sources"]})), clock.is_scenario(self.plan)))
        self.plan.commit()
        return decision_id

    def _records(self, promise, part, state, card, rec, why, category, site, decision_id, now) -> list[dict]:
        supplier, lane = promise["supplier_id"], promise.get("lane") or part
        shared = {"supplier_id": supplier, **({"lane": lane} if lane else {})}
        records = [{**shared, "kind": "promise", "promise_id": promise["promise_id"], "state": "broken",
                    "what": promise["what"], "due": promise["due"].isoformat(),
                    "broken_at": promise["broken_at"].isoformat(), "sources": promise["sources"]}]
        if card:
            derived = ("id", "producer", "history")
            records.append({**{k: v for k, v in card.items() if k not in derived}, "kind": "supplier"})
        if lane:    # one card per lane: an older one may still offer the supplier that just failed
            records += [{"kind": "decision", "decision_id": d["decision_id"], "status": "superseded"}
                        for d in state["decisions"].values()
                        if d.get("status") == "open" and d.get("lane") in (lane, part)
                        and d["decision_id"] != decision_id]
        title = f"{supplier} broke its promise: {promise['what']}"
        decision = {**shared, "kind": "decision", "decision_id": decision_id, "status": "open",
                    "at": now.isoformat(), "sources": promise["sources"], "actions": []}
        if rec:
            verb = "Switch to" if rec["kind"] == "hedge" else "Do not switch to"
            decision.update(
                title=f"{title}. {verb} {rec['backup_supplier_id']}?", recommendation=rec["rationale"],
                recommendation_id=rec["recommendation_id"], backup_supplier_id=rec["backup_supplier_id"],
                offer_id=rec["offer_id"], decide_by=rec["decide_by"], decide_by_why=rec["decide_by_why"],
                actions=rec["actions"], category=category, delivery_site=site, signer=card_signer(rec["approval"]),
                hedge=rec["hedge"], drilldown=rec["drilldown"], sources=[*promise["sources"], *rec["sources"]])
        else:
            decision.update(title=f"{title}. A person has to re-plan.",
                            recommendation=f"No backup can be proposed: {why}. "
                                           f"Fallback on file: {promise.get('fallback') or 'none'}.")
        records.append({**shared, "kind": "event", "type": HANDLED, "severity": "critical", "title": title,
                        "at": promise["broken_at"].isoformat(), "decision_id": decision_id,
                        "source": promise["sources"][-1] if promise["sources"] else None,
                        "detail": {"happened": title,
                                   "reasoning": [{"text": "Re-plan", "value": decision["recommendation"]}]}})
        return _plain([*records, decision])


def _mail_tools():
    """Draft-only mail tools for the broker when the mail layer is configured; none otherwise (sending is never
    exposed to the broker, see agents.tools.mail_tools)."""
    try:
        from mail.tools import MailTools
        return MailTools(actor="agent:broker")
    except (Exception, SystemExit) as exc:      # mail settings missing: the broker still registers promises
        log.info("broker runs without mail tools (%s)", type(exc).__name__)
        return None


def run(url: str, client: llm.LLMClient, log_path: Path, stop: threading.Event, *, kb_schema: str = "kb",
        idle: float = 1.0) -> None:
    """Follow kb and mail under a singleton lock. A lost connection backs off, reconnects and takes it again."""
    failures = 0
    while not stop.is_set():
        conns = []
        try:
            listen = plan_db.connect(url, autocommit=True)
            conns.append(listen)
            if listen.execute("SELECT pg_try_advisory_lock(%s) AS ok", (LOCK,)).fetchone()["ok"]:
                plan = plan_db.connect(url)
                conns.append(plan)
                kb = kb_db.connect(url, kb_schema)
                conns.append(kb)
                monitor = Monitor(plan, kb, client, log_path, kb_schema=kb_schema, mail=_mail_tools())
                for channel in (kb_events.FACT_CHANNEL, kb_db.ISSUE_CHANNEL, mail_db.NOTIFY_CHANNEL):
                    listen.execute(f"LISTEN {channel}")
                while not stop.is_set():
                    for decision_id in monitor.tick():
                        log.info("published decision %s", decision_id)
                    failures = 0
                    for _ in listen.notifies(timeout=idle, stop_after=1):
                        pass
                return
            log.info("another monitor holds the lock")
        except (psycopg.OperationalError, psycopg.InterfaceError, psycopg.errors.UndefinedTable, OSError) as exc:
            log.warning("monitor cannot read the database or write the log (%s)", type(exc).__name__)
        finally:
            for conn in conns:
                conn.close()
        stop.wait(BACKOFF[min(failures, len(BACKOFF) - 1)])
        failures += 1
