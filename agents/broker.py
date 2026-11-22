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
from datetime import datetime, timezone
from typing import Iterable

from psycopg import sql
from psycopg.types.json import Json

from sourcing import promises

from . import llm, tools
from .tools import ToolRegistry

# plan.checkins.outcome is a closed vocabulary; a reconcile outcome maps onto it.
_CHECKIN_OUTCOME = {promises.PROVEN: "proven", promises.BROKEN: "broken"}

# What the broker role may set when it registers a promise. `made_at` is bound to the run's clock, not the model.
REGISTER_SCHEMA = {
    "type": "object",
    "required": ["promise_id", "supplier_id", "what", "fallback"],
    "properties": {
        "promise_id": {"type": "string"},
        "supplier_id": {"type": "string"},
        "what": {"type": "string", "description": "the commitment being watched, e.g. 'loads the backup order'"},
        "within": {"type": "string", "description": "the quoted duration from the statement, e.g. 'within 4 h'"},
        "due": {"type": "string", "description": "an absolute ISO 8601 due time (use instead of `within`)"},
        "grace_h": {"type": "number", "description": "hours after due before the first check (default 0)"},
        "fallback": {"type": "string", "description": "what to do the moment the promise breaks"},
        "part_id": {"type": "string"},
        "lane": {"type": "string"},
        "quantity": {"type": "number"},
        "proof": {"type": "string"},
        "decision_id": {"type": "string"},
        "sources": {"type": "array", "items": {}, "description": "the kb fact id(s) the promise rests on"},
    },
}

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


def statements_from_rows(rows: Iterable[dict]) -> list[promises.Statement]:
    """Group `kb.current_facts` rows of a supplier statement into one `Statement` each. A statement is a subject
    (statement_no + material_id, and supplier_id when the kb carries it) with a `kind` and the quoted `value`
    text. `at` is when the supplier said it (`as_of`) or when the kb saw it (`observed_at`)."""
    grouped: dict[str, dict] = {}
    for row in rows:
        if row.get("fact_type") != "supplier_statement":
            continue
        subject = row.get("subject") or {}
        key = json.dumps(subject, sort_keys=True, default=str)
        rec = grouped.setdefault(key, {"subject": subject, "attrs": {}, "rows": []})
        rec["attrs"][row.get("attribute")] = row.get("value")
        rec["rows"].append(row)
    out: list[promises.Statement] = []
    for rec in grouped.values():
        attrs, subject, group_rows = rec["attrs"], rec["subject"], rec["rows"]
        kind = attrs.get("kind")
        if not kind:
            continue
        kind_row = next((r for r in group_rows if r.get("attribute") == "kind"), group_rows[0])
        at = kind_row.get("as_of") or kind_row.get("observed_at")
        instant = _instant_or_none(at)
        out.append(promises.Statement(
            kind=str(kind),
            at=instant.isoformat() if instant else str(at),
            supplier_id=subject.get("supplier_id"),
            part_id=subject.get("material_id") or subject.get("part_id"),
            lane=subject.get("lane"),
            text=attrs.get("value"),
            fact_id=kind_row.get("fact_id"),
        ))
    return out


def read_statements(kb_conn) -> list[promises.Statement]:
    """Every current supplier statement in the kb, as ledger `Statement`s. `kb_conn` has search_path on kb."""
    rows = kb_conn.execute(
        "SELECT fact_id, fact_type, subject, attribute, value, as_of, observed_at "
        "FROM current_facts WHERE fact_type = 'supplier_statement'").fetchall()
    return statements_from_rows(rows)


# ---- the plan.promises ledger ------------------------------------------------

_PROMISE_COLS = ("promise_id", "supplier_id", "part_id", "lane", "decision_id", "what", "quantity",
                 "due", "proof", "state", "next_check", "fallback", "sources")


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
    """The promises still waiting, with the keys the ledger needs to match and time statements against."""
    return plan_conn.execute(
        "SELECT promise_id, supplier_id, part_id, lane, what, due, next_check, fallback, state, created_at "
        "FROM promises WHERE state = 'waiting' ORDER BY promise_id").fetchall()


def apply_ledger(plan_conn, statements: Iterable[promises.Statement], *,
                 outcomes: dict[str, str] = promises.OUTCOMES) -> list[dict]:
    """Fold the statements onto the open promises and write each proof or break back: the promise's new state and
    time, the citing source appended, and a check-in row. Returns the updates applied. The caller commits."""
    waiting = open_promises(plan_conn)
    updates = promises.reconcile(waiting, statements, outcomes=outcomes)
    for upd in updates:
        at = upd.get("proven_at") or upd.get("broken_at")
        at_col = "proven_at" if upd["state"] == promises.PROVEN else "broken_at"
        plan_conn.execute(
            sql.SQL("UPDATE promises SET state = %s, {at_col} = %s, sources = sources || %s::jsonb, "
                    "updated_at = now() WHERE promise_id = %s").format(at_col=sql.Identifier(at_col)),
            (upd["state"], at, Json([upd["source"]]), upd["promise_id"]))
        plan_conn.execute(
            "INSERT INTO checkins (promise_id, at, outcome, note, evidence) VALUES (%s, %s, %s, %s, %s)",
            (upd["promise_id"], at, _CHECKIN_OUTCOME[upd["state"]],
             f"supplier statement: {upd['statement_kind']}", Json([upd["source"]])))
    return updates


# ---- the broker role (LLM) ---------------------------------------------------

def _register_tool(plan_conn, now_iso: str, registered: list[dict]):
    def register_promise(**kwargs):
        kwargs["made_at"] = now_iso                       # the clock is the run's, never the model's
        promise = promises.promise_from_statement(**kwargs)
        save_promise(plan_conn, promise)
        registered.append(promise)
        return promise
    return register_promise


def broker_registry(plan_conn, mail, *, now_iso: str, registered: list[dict]) -> ToolRegistry:
    """The tools the broker role is given: a grounded promise register (plan) and the MailTools draft/read tools.
    The send path is never exposed (`tools.mail_tools` drops it)."""
    registry = ToolRegistry()
    registry.add("register_promise",
                 "Register a watched promise from a supplier statement. The due time and next check are computed "
                 "in code from the quoted `within` (or an absolute `due`); you pass the words, not the arithmetic.",
                 REGISTER_SCHEMA, _register_tool(plan_conn, now_iso, registered))
    for tool in tools.mail_tools(mail):
        registry.register(tool)
    return registry


def run_broker(client: llm.LLMClient, plan_conn, mail, *, prompt: str, now, system: str = SYSTEM,
               tracer=None, max_turns: int = 12) -> tuple[llm.AgentResult, list[dict]]:
    """Drive the broker: register promises and draft the hedge. Returns the agent result and the promises it
    registered (already saved to plan.promises). `now` sets the promises' clock (a datetime or ISO string)."""
    now_iso = now.astimezone(timezone.utc).isoformat() if isinstance(now, datetime) else str(now)
    registered: list[dict] = []
    registry = broker_registry(plan_conn, mail, now_iso=now_iso, registered=registered)
    result = llm.run(client, registry, role="broker", system=system, prompt=prompt,
                     schema=None, guard=None, tracer=tracer, max_turns=max_turns)
    return result, registered
