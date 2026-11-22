"""The promise broker (role F4) and the ledger behind it.

Two halves, one contract (`plan.promises`):
- the ledger (`read_statements`, `apply_ledger`) is deterministic: it reads the supplier statements the kb already
  extracted (`fact_type = 'supplier_statement'`), folds them onto the open promises with `sourcing.promises`, and
  writes each proof or break back. There is no second email parser here: the broker consumes kb facts, it does not
  read email.
- the broker role (`run_broker`) is the LLM half: it registers a watched promise from a supplier statement through
  a grounded tool (the due time and the next check are computed in code, not by the model) and drafts a backup
  offer and a follow-up through `MailTools`. Drafts are stored, never sent: a human approves and sends, by name
  (AGENTS.md), so in tests the mail layer runs in dry-run.

Every number a promise carries comes from the deterministic tool, and every promise cites the kb fact it rests on.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Iterable

from psycopg import sql
from psycopg.types.json import Json

from sourcing import promises

from . import llm, tools
from .tools import ToolRegistry

# plan.checkins.outcome is a closed vocabulary; a reconcile outcome maps onto it.
_CHECKIN_OUTCOME = {promises.PROVEN: "proven", promises.BROKEN: "broken"}

# What the broker role may set when it registers a promise. It passes words and the cited fact; the code supplies
# every number. `made_at`, the quantity and the next check are not the model's to set: made_at and the quantity
# come from the cited statement, the check is computed from the due time. `additionalProperties` is closed so a
# stray `next_check` or `quantity` from the model is rejected rather than stored.
REGISTER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["promise_id", "what", "fallback", "sources"],
    "properties": {
        "promise_id": {"type": "string"},
        "supplier_id": {"type": "string", "description": "the supplier; taken from the cited statement when it has one"},
        "what": {"type": "string", "description": "the commitment being watched, e.g. 'loads the backup order'"},
        "within": {"type": "string", "description": "the quoted duration from the statement, e.g. 'within 4 h'"},
        "due": {"type": "string", "description": "an absolute ISO 8601 due time (only when there is no `within`)"},
        "grace_h": {"type": "number", "description": "hours after due before the first check (default 0)"},
        "fallback": {"type": "string", "description": "what to do the moment the promise breaks"},
        "part_id": {"type": "string"},
        "lane": {"type": "string"},
        "proof": {"type": "string"},
        "decision_id": {"type": "string"},
        "sources": {"type": "array", "items": {}, "minItems": 1,
                    "description": "the kb fact id(s) the promise rests on; at least one is required"},
    },
}

# The model passes words and the cited fact; everything else the code decides. Keys outside this set are dropped
# before the deterministic tool runs, so the model cannot inject a due time, a quantity or a next check.
_REGISTER_KEYS = frozenset({"promise_id", "supplier_id", "what", "within", "due", "grace_h",
                            "fallback", "part_id", "lane", "proof", "decision_id", "sources"})

SYSTEM = (
    "You are the promise broker. A supplier statement is a commitment to watch (for example 'loads within 4 h'). "
    "For each one: call register_promise with the supplier, what is promised, the quoted duration verbatim in "
    "`within` (do not compute the due time yourself), and a concrete fallback. Then hedge it: draft a backup "
    "offer to an alternate supplier and a short follow-up to the promising supplier through draft_email. Never "
    "send: a human approves and sends. Quote numbers only as the statement gave them; the tools compute the rest. "
    "Answer with a JSON object {promises: [ids], drafts: [draft_ids], note}."
)


# ---- reading the kb's supplier statements ------------------------------------

def _instant_or_none(value):
    if value is None:
        return None
    try:
        from sourcing import timing
        return timing.instant(value)
    except Exception:
        return None


def _supplier_of(locator, subject) -> str | None:
    """The supplier a statement is from. A supplier email carries it in `locator.mail` (supplier_id, or the
    persona_id when the mail is a scenario persona and supplier_id is null, kb/mail_source.py). A tabular fact may
    instead carry it in the subject."""
    if isinstance(locator, dict):
        mail = locator.get("mail")
        if isinstance(mail, dict):
            sup = mail.get("supplier_id") or mail.get("persona_id")
            if sup is not None:
                return str(sup)
    if isinstance(subject, dict) and subject.get("supplier_id") is not None:
        return str(subject["supplier_id"])
    return None


def _statement_time(row: dict) -> str | None:
    """When the supplier made the statement, as an ISO string, or None when no real time is known. The
    supplier-stated time (`as_of`) first, then a document or mail time carried on the locator; never the kb load
    time (`observed_at`), which is today's clock and would break any promise made before the load and settle
    nothing once the scenario clock passes it (finding: the kb load time was used as the statement time). None
    lets the ledger treat the time as unknown and surface it for a human rather than settle on the wrong clock."""
    candidates = [row.get("as_of")]
    locator = row.get("locator")
    if isinstance(locator, dict):
        mail = locator.get("mail")
        if isinstance(mail, dict):
            candidates += [mail.get("date_header"), mail.get("sent_at"), mail.get("date")]
        candidates.append(locator.get("at"))
    for cand in candidates:
        inst = _instant_or_none(cand)
        if inst is not None:
            return inst.isoformat()
    return None


def statements_from_rows(rows: Iterable[dict]) -> list[promises.Statement]:
    """Group `kb.current_facts` rows of a supplier statement into one `Statement` each. Per the extraction
    contract the subject is `statement_no`, while `material_id`, `kind`/`promise_kind`, `value`, `due_time` and
    `quantity` are attributes, and the supplier is in `locator.mail`. Rows are grouped by document and subject so
    two emails that reuse a statement number do not merge and cite each other's fact (finding: different emails
    merge). Each statement keeps every grouped fact id (`fact_ids`) so the ledger still recognises it as the maker
    of a promise however the register tool cited it. `at` is the supplier-stated or document time, never the kb
    load time (see `_statement_time`)."""
    grouped: dict[str, dict] = {}
    for row in rows:
        if row.get("fact_type") != "supplier_statement":
            continue
        subject = row.get("subject") or {}
        doc = (row.get("path"), row.get("sha256"))
        key = json.dumps([doc, subject], sort_keys=True, default=str)
        rec = grouped.setdefault(key, {"subject": subject, "attrs": {}, "rows": []})
        rec["attrs"][row.get("attribute")] = row.get("value")
        rec["rows"].append(row)
    out: list[promises.Statement] = []
    for rec in grouped.values():
        attrs, subject, group_rows = rec["attrs"], rec["subject"], rec["rows"]
        kind = attrs.get("kind") or attrs.get("promise_kind")
        if not kind:
            continue
        kind_row = next((r for r in group_rows if r.get("attribute") in ("kind", "promise_kind")), group_rows[0])
        fact_ids = tuple(r.get("fact_id") for r in group_rows if r.get("fact_id") is not None)
        out.append(promises.Statement(
            kind=str(kind),
            at=_statement_time(kind_row),
            supplier_id=_supplier_of(kind_row.get("locator"), subject),
            part_id=attrs.get("material_id") or attrs.get("part_id") or subject.get("material_id"),
            lane=attrs.get("lane") or subject.get("lane"),
            text=attrs.get("value"),
            fact_id=kind_row.get("fact_id"),
            due_time=attrs.get("due_time"),
            quantity=attrs.get("quantity"),
            fact_ids=fact_ids,
        ))
    return out


def read_statements(kb_conn) -> list[promises.Statement]:
    """Every current supplier statement in the kb, as ledger `Statement`s. `kb_conn` has search_path on kb."""
    rows = kb_conn.execute(
        "SELECT fact_id, fact_type, subject, attribute, value, as_of, locator, path, sha256 "
        "FROM current_facts WHERE fact_type = 'supplier_statement'").fetchall()
    return statements_from_rows(rows)


# ---- the plan.promises ledger ------------------------------------------------

# `created_at` carries the promise's made time: the table has no made_at column, so persisting it here keeps the
# ledger's clock correct after a reload (it reads made_at, then created_at, then due).
_PROMISE_COLS = ("promise_id", "supplier_id", "part_id", "lane", "decision_id", "what", "quantity",
                 "due", "proof", "state", "next_check", "fallback", "sources", "created_at")


def save_promise(plan_conn, promise: dict) -> None:
    """Insert or refresh one promise. `plan_conn` has search_path on the plan schema."""
    values = []
    for col in _PROMISE_COLS:
        value = promise.get(col)
        values.append(Json(value) if col == "sources" else value)
    assignments = sql.SQL(", ").join(
        sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in _PROMISE_COLS if c != "promise_id")
    plan_conn.execute(
        sql.SQL("INSERT INTO promises ({cols}) VALUES ({ph}) "
                "ON CONFLICT (promise_id) DO UPDATE SET {upd}, updated_at = now()").format(
            cols=sql.SQL(", ").join(sql.Identifier(c) for c in _PROMISE_COLS),
            ph=sql.SQL(", ").join(sql.Placeholder() * len(_PROMISE_COLS)),
            upd=assignments),
        values)


def open_promises(plan_conn) -> list[dict]:
    """The promises still waiting, with the keys the ledger needs to match and time statements against. `created_at`
    carries the made time (there is no made_at column), so the ledger's clock survives a reload."""
    return plan_conn.execute(
        "SELECT promise_id, supplier_id, part_id, lane, what, due, next_check, fallback, state, created_at, sources "
        "FROM promises WHERE state = 'waiting' ORDER BY promise_id").fetchall()


def apply_ledger(plan_conn, statements: Iterable[promises.Statement], *,
                 outcomes: dict[str, str] = promises.OUTCOMES) -> list[dict]:
    """Fold the statements onto the open promises and write each proof or break back: the promise's new state and
    time, the citing source appended, and a check-in row (its outcome is `late` when a proof lands after the due
    time). Returns the updates applied. Each write is isolated in a savepoint so one bad row does not abort the
    others. The caller commits. Statements that bear on several promises or whose time cannot be read settle
    nothing here; `promises.review` surfaces them."""
    waiting = open_promises(plan_conn)
    updates = promises.reconcile(waiting, statements, outcomes=outcomes)
    applied: list[dict] = []
    for upd in updates:
        at = upd.get("proven_at") or upd.get("broken_at")
        at_col = "proven_at" if upd["state"] == promises.PROVEN else "broken_at"
        outcome = "late" if upd.get("late") else _CHECKIN_OUTCOME[upd["state"]]
        try:
            with plan_conn.transaction():
                plan_conn.execute(
                    sql.SQL("UPDATE promises SET state = %s, {at_col} = %s, sources = sources || %s::jsonb, "
                            "updated_at = now() WHERE promise_id = %s").format(at_col=sql.Identifier(at_col)),
                    (upd["state"], at, Json([upd["source"]]), upd["promise_id"]))
                plan_conn.execute(
                    "INSERT INTO checkins (promise_id, at, outcome, note, evidence) VALUES (%s, %s, %s, %s, %s)",
                    (upd["promise_id"], at, outcome,
                     f"supplier statement: {upd['statement_kind']}", Json([upd["source"]])))
        except Exception:
            continue
        applied.append(upd)
    return applied


# ---- the broker role (LLM) ---------------------------------------------------

_SETTLED = (promises.PROVEN, promises.BROKEN, promises.CANCELLED)
_QTY = re.compile(r"-?\d+(?:[.,]\d+)?")


def parse_quantity(text) -> float | None:
    """A quantity parsed in code from verbatim text (the probe finding: "10 000" was misread when the model did
    the arithmetic). Spaces group digits and are dropped; a comma is a decimal point. Returns None when there is
    no number to read, so the model can never inject a quantity."""
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text)
    if not isinstance(text, str):
        return None
    cleaned = re.sub(r"[\s ]", "", text)
    m = _QTY.search(cleaned)
    if not m:
        return None
    try:
        return float(m.group().replace(",", "."))
    except ValueError:
        return None


def _statement_for(sources, by_fact: dict) -> promises.Statement | None:
    """The cited statement for a register call, matched by the first cited fact id that is a known supplier
    statement. `by_fact` is keyed by every fact id of each statement, so citing any fact of the email resolves it.
    Returns None when no cited id is a known statement, so the register tool can refuse invented ids."""
    for src in sources or []:
        fid = src.get("fact_id") if isinstance(src, dict) else src
        if fid is not None and str(fid) in by_fact:
            return by_fact[str(fid)]
    return None


def _register_tool(plan_conn, now_iso: str, registered: list[dict], by_fact: dict):
    def register_promise(**kwargs):
        kwargs = {k: v for k, v in kwargs.items() if k in _REGISTER_KEYS}   # the code owns every number
        sources = kwargs.get("sources") or []
        if not sources:
            return {"error": "a promise must cite the kb fact it rests on (sources)"}
        pid = str(kwargs.get("promise_id") or "")
        existing = plan_conn.execute("SELECT state FROM promises WHERE promise_id = %s", (pid,)).fetchone()
        if existing and existing["state"] in _SETTLED:
            return {"error": f"promise {pid!r} is already {existing['state']}; a settled promise is not reopened"}
        st = _statement_for(sources, by_fact)
        if st is None:
            # Refuse an invented fact id: a promise must rest on a supplier statement the kb actually holds, or the
            # model could register a watched promise (with a model-written due) on nothing (finding: not grounded).
            return {"error": "none of the cited facts is a known supplier statement; a promise must cite one"}
        # The clock, the quantity and the supplier come from the cited statement, not the model: made_at is when the
        # supplier spoke (the run clock only when the statement itself has no readable time), the quantity is parsed
        # in code from the statement's verbatim text, and the supplier is the statement's, overriding any the model
        # passed (the schema says so, and a model must not reassign a statement to another supplier).
        kwargs["made_at"] = st.at or now_iso
        qty = parse_quantity(st.quantity)
        if qty is not None:
            kwargs["quantity"] = qty
        if st.supplier_id:
            kwargs["supplier_id"] = str(st.supplier_id)
        if not kwargs.get("supplier_id"):
            return {"error": "no supplier: the cited statement has none and none was given"}
        if kwargs.get("within"):
            kwargs.pop("due", None)        # a quoted duration is grounded; an absolute due from the model is not
        try:
            promise = promises.promise_from_statement(**kwargs)
        except promises.PromiseError as exc:
            return {"error": str(exc)}
        try:
            with plan_conn.transaction():
                save_promise(plan_conn, promise)
        except Exception as exc:           # one bad call (e.g. an unknown decision_id) must not lose the others
            return {"error": f"could not save promise {pid!r}: {type(exc).__name__}: {exc}"}
        registered.append(promise)
        return promise
    return register_promise


def broker_registry(plan_conn, mail, *, now_iso: str, registered: list[dict],
                    statements: Iterable[promises.Statement] = ()) -> ToolRegistry:
    """The tools the broker role is given: a grounded promise register (plan) and the MailTools draft/read tools.
    The send path is never exposed (`tools.mail_tools` drops it). The `statements` the broker is briefing on let
    the register tool take the made time and the quantity from the cited kb fact, not from the model."""
    by_fact: dict = {}
    for s in statements:
        for fid in s.all_fact_ids():       # any fact of the email resolves to its statement
            by_fact.setdefault(fid, s)
    registry = ToolRegistry()
    registry.add("register_promise",
                 "Register a watched promise from a supplier statement. Pass the words and cite the kb fact in "
                 "`sources`; the due time, the next check, the made time and any quantity are computed in code "
                 "from the quoted `within` (or an absolute `due`) and the cited statement, never by you.",
                 REGISTER_SCHEMA, _register_tool(plan_conn, now_iso, registered, by_fact))
    for tool in tools.mail_tools(mail):
        registry.register(tool)
    return registry


def run_broker(client: llm.LLMClient, plan_conn, mail, *, prompt: str, now, system: str = SYSTEM,
               statements: Iterable[promises.Statement] = (), tracer=None,
               max_turns: int = 12) -> tuple[llm.AgentResult, list[dict]]:
    """Drive the broker: register promises and draft the hedge. Returns the agent result and the promises it
    registered (already saved to plan.promises). `now` sets the fallback clock (a datetime or ISO string) when a
    call cites no statement; `statements` are the kb statements the broker is briefing on, so a registered promise
    takes its made time and quantity from the fact it cites. The answer's `promises` list is reconciled to the ids
    actually registered, so it cannot name a promise that does not exist."""
    now_iso = now.astimezone(timezone.utc).isoformat() if isinstance(now, datetime) else str(now)
    registered: list[dict] = []
    statements = list(statements)
    registry = broker_registry(plan_conn, mail, now_iso=now_iso, registered=registered, statements=statements)
    result = llm.run(client, registry, role="broker", system=system, prompt=prompt,
                     schema=None, guard=None, tracer=tracer, max_turns=max_turns)
    if isinstance(result.verdict, dict):
        result.verdict["promises"] = [p["promise_id"] for p in registered]
    return result, registered
