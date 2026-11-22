"""Promise ledger: turn a supplier statement into a watched promise, and fold later statements onto the open
promises to prove or break them. Pure functions, no clock of their own, no DB, no LLM: `now` and every time are
passed in. A duration ("within 4 h") is quoted verbatim and parsed here, never guessed (the probe finding), so
the due time is computed in code, not by a model.

    p = promise_from_statement(promise_id="PR-1", supplier_id="SUP-A", what="loads the backup order",
                               made_at=now, within="within 4 h", fallback="call the contact", sources=[fact_src])
    #   -> {"due": now+4h, "next_check": now+4h, "state": "waiting", "fallback": ...}
    updates = reconcile([p], [Statement(kind="dispatch_info", at=later, supplier_id="SUP-A", fact_id="f-9")])
    #   -> [{"promise_id": "PR-1", "state": "proven", "proven_at": later, "source": {"fact_id": "f-9"}}]

Matching a statement to a promise is deterministic: every key the two both name (supplier, part, lane) must be
equal, and at least one must be shared. The earliest decisive statement at or after the promise was made wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Iterable

from plan import contracts

from sourcing import timing

WAITING, PROVEN, BROKEN, CANCELLED = "waiting", "proven", "broken", "cancelled"

# How a supplier_statement kind moves an open promise. Two vocabularies land here, both from the kb (not Soony
# data): the statement `kind` (dispatch_info, on_hold, ...) and the contract's `promise_kind` (confirm, delay,
# cancel, short-ship). A kind not listed is informational and leaves the promise waiting. Callers may pass their
# own map to `reconcile`.
OUTCOMES: dict[str, str] = {
    "dispatch_info": PROVEN,
    "confirm": PROVEN,
    "on_hold": BROKEN,
    "delay": BROKEN,
    "short_shipment": BROKEN,
    "short-ship": BROKEN,
    "cancel": BROKEN,
    "cancelled": BROKEN,
    "term_rejected": BROKEN,
}

_MATCH_KEYS = ("supplier_id", "part_id", "lane")


class PromiseError(ValueError):
    pass


@dataclass(frozen=True)
class Statement:
    """A supplier statement as the kb recorded it (fact_type `supplier_statement`), normalized for the ledger.
    `kind` is the statement kind (or the contract's `promise_kind`); `at` is when the supplier said it; `fact_id`
    cites the kb fact it came from. `due_time` and `quantity` are kept verbatim as the kb has them and parsed in
    code by the caller, never guessed (the probe finding)."""
    kind: str
    at: str
    supplier_id: str | None = None
    part_id: str | None = None
    lane: str | None = None
    text: str | None = None
    fact_id: str | int | None = None
    due_time: str | None = None
    quantity: str | None = None

    def source(self) -> dict:
        if self.fact_id is not None:
            return {"fact_id": self.fact_id}
        return {"label": "supplier_statement", "value": self.text or self.kind}


def promise_from_statement(*, promise_id: str, supplier_id: str, what: str, made_at, fallback: str,
                           due=None, within: str | None = None, grace_h: float = 0.0, next_check=None,
                           part_id: str | None = None, lane: str | None = None, quantity=None,
                           proof: str | None = None, decision_id: str | None = None,
                           sources: Iterable | None = None) -> dict:
    """A watched promise from a supplier statement. Give the due time directly (`due`) or a quoted duration
    (`within`, e.g. "within 4 h"); the check is the due time plus `grace_h` unless `next_check` is given. A
    promise with no fallback is refused: nothing may be watched that nobody can act on when it breaks."""
    if not (isinstance(fallback, str) and fallback.strip()):
        raise PromiseError("a promise needs a fallback: what to do the moment it breaks")
    if not (isinstance(what, str) and what.strip()):
        raise PromiseError("a promise needs a `what`: the commitment being watched")
    made = timing.instant(made_at).astimezone(timezone.utc)
    if due is not None:
        due_dt = timing.instant(due).astimezone(timezone.utc)
    elif within is not None:
        due_dt = made + timedelta(hours=timing.parse_hours(within))
    else:
        raise PromiseError("a promise needs a due time: pass `due` or a quoted `within` such as 'within 4 h'")
    if next_check is not None:
        check_dt = timing.instant(next_check).astimezone(timezone.utc)
    else:
        if not isinstance(grace_h, (int, float)) or isinstance(grace_h, bool) or grace_h < 0:
            raise PromiseError(f"grace_h must be a number of hours >= 0, got {grace_h!r}")
        check_dt = due_dt + timedelta(hours=float(grace_h))
    promise = {
        "promise_id": str(promise_id),
        "supplier_id": str(supplier_id),
        "what": str(what),
        "made_at": made.isoformat(),
        # created_at carries the made time into the row: the plan.promises table has no made_at column, so without
        # this a reloaded promise would anchor the ledger on the database's wall clock (finding: the stored ledger
        # uses the wrong clock). `_reference_time` reads made_at, then created_at, then due.
        "created_at": made.isoformat(),
        "due": due_dt.isoformat(),
        "state": WAITING,
        "next_check": check_dt.isoformat(),
        "fallback": fallback,
        "sources": list(sources or []),
    }
    for key, value in (("part_id", part_id), ("lane", lane), ("quantity", quantity),
                       ("proof", proof), ("decision_id", decision_id)):
        if value is not None:
            promise[key] = value
    contracts.validate("promise", promise)
    return promise


def _matches(promise: dict, st: Statement) -> bool:
    shared = False
    for key in _MATCH_KEYS:
        pv, sv = promise.get(key), getattr(st, key)
        if pv is not None and sv is not None:
            if str(pv) != str(sv):
                return False
            shared = True
    return shared


def _source_fact_id(source) -> str | None:
    """The kb fact id a source cites, however the register tool stored it: a bare id, `{"fact_id": id}`, or a
    richer dict that also carries a fact id. Returns None for a source that names no fact (a label/value pair)."""
    if isinstance(source, dict):
        fid = source.get("fact_id")
        return str(fid) if fid is not None else None
    if isinstance(source, (str, int)) and not isinstance(source, bool):
        return str(source)
    return None


def _made_the_promise(promise: dict, st: Statement) -> bool:
    """True when the statement is one the promise already rests on: the statement that established the
    commitment. Such a statement must not also settle the promise it made, or a confirm would prove its own
    promise and a later cancel could never break it (the citing statement is in every ledger run, since
    `read_statements` returns every current statement). Matched by kb fact id across the shapes the register
    tool accepts, with a fallback to exact source identity when the statement carries no fact id."""
    sources = promise.get("sources") or []
    if st.fact_id is not None:
        fid = str(st.fact_id)
        return any(_source_fact_id(s) == fid for s in sources)
    return st.source() in sources


def _reference_time(promise: dict):
    """The moment from which a statement can bear on a promise: when it was made, else created, else its due."""
    for key in ("made_at", "created_at", "due"):
        value = promise.get(key)
        if value:
            try:
                return timing.instant(value)
            except timing.TimingError:
                continue
    return None


def _timed(statements: Iterable[Statement]) -> tuple[list[tuple[Statement, "object"]], list[Statement]]:
    """Split statements into (readable time, the parsed instant) and the ones whose `at` cannot be read. A single
    unreadable time must not stop the whole ledger (finding: one row stops the whole ledger)."""
    readable: list[tuple[Statement, object]] = []
    unreadable: list[Statement] = []
    for st in statements:
        try:
            readable.append((st, timing.instant(st.at)))
        except (timing.TimingError, TypeError, ValueError):
            unreadable.append(st)
    readable.sort(key=lambda pair: pair[1])
    return readable, unreadable


def _bearing(waiting: list[dict], statements: Iterable[Statement], outcomes: dict[str, str]):
    """For each decisive, time-readable statement, the open promises it bears on (matches and is at or after the
    promise was made). Yields (statement, instant, outcome, [promises])."""
    readable, _ = _timed(statements)
    for st, inst in readable:
        outcome = outcomes.get(st.kind)
        if outcome is None:
            continue
        hits = []
        for promise in waiting:
            if not _matches(promise, st):
                continue
            if _made_the_promise(promise, st):
                continue       # the statement that made the promise does not also settle it
            since = _reference_time(promise)
            if since is not None and inst < since:
                continue
            hits.append(promise)
        yield st, inst, outcome, hits


def reconcile(promises: Iterable[dict], statements: Iterable[Statement], *,
              outcomes: dict[str, str] = OUTCOMES) -> list[dict]:
    """For each waiting promise, the state change the supplier statements force, or nothing. The earliest decisive
    statement (proven or broken) at or after the promise was made wins. A statement that bears on more than one
    open promise is ambiguous and settles none of them (it is surfaced by `review`); informational statements,
    statements for a different supplier/part/lane, and statements whose time cannot be read are ignored here.
    A proof that lands after the due time carries `late` so lateness stays visible. Deterministic, no side effects."""
    statements = list(statements)
    waiting = [p for p in promises if p.get("state", WAITING) == WAITING]
    # Only unambiguous statements (bearing on exactly one open promise) may settle it.
    per_promise: dict[str, list[tuple[object, Statement, str]]] = {}
    for st, inst, outcome, hits in _bearing(waiting, statements, outcomes):
        if len(hits) != 1:
            continue
        per_promise.setdefault(hits[0]["promise_id"], []).append((inst, st, outcome))
    updates: list[dict] = []
    for promise in waiting:
        cands = per_promise.get(promise["promise_id"])
        if not cands:
            continue
        inst, st, outcome = min(cands, key=lambda c: c[0])
        at_key = "proven_at" if outcome == PROVEN else "broken_at"
        update = {"promise_id": promise["promise_id"], "state": outcome, at_key: st.at,
                  "source": st.source(), "statement_kind": st.kind}
        due = _instant_or_none(promise.get("due"))
        if outcome == PROVEN and due is not None and inst > due:
            update["late"] = True          # proven, but after the deadline: the check-in says so
        updates.append(update)
    return updates


def _instant_or_none(value):
    if not value:
        return None
    try:
        return timing.instant(value)
    except (timing.TimingError, TypeError, ValueError):
        return None


def review(promises: Iterable[dict], statements: Iterable[Statement], *,
           outcomes: dict[str, str] = OUTCOMES) -> list[dict]:
    """Decisive statements that cannot be applied automatically and need a human: `ambiguous` (one statement bears
    on several open promises, e.g. a dispatch that names only the supplier) and `unreadable_time` (the statement's
    time cannot be read). Pure: it reports, it does not change anything."""
    statements = list(statements)
    waiting = [p for p in promises if p.get("state", WAITING) == WAITING]
    out: list[dict] = []
    for st, inst, outcome, hits in _bearing(waiting, statements, outcomes):
        if len(hits) > 1:
            out.append({"reason": "ambiguous", "statement_kind": st.kind, "at": st.at,
                        "source": st.source(), "candidates": [p["promise_id"] for p in hits]})
    _, unreadable = _timed(statements)
    for st in unreadable:
        if outcomes.get(st.kind) is None:
            continue
        if any(_matches(p, st) for p in waiting):
            out.append({"reason": "unreadable_time", "statement_kind": st.kind, "at": st.at,
                        "source": st.source(),
                        "candidates": [p["promise_id"] for p in waiting if _matches(p, st)]})
    return out


def apply_update(promise: dict, update: dict) -> dict:
    """The promise after a reconcile update: its new state, the proof/break time, and the citing source added."""
    out = dict(promise)
    for key in ("state", "proven_at", "broken_at"):
        if key in update:
            out[key] = update[key]
    source = update.get("source")
    if source is not None:
        out["sources"] = [*out.get("sources", []), source]
    return out


def watch_gaps(promises: Iterable[dict]) -> list[str]:
    """The ids of open promises that are not fully watched: no next check, or no fallback. Empty is the goal:
    every promise still waiting can be checked and has something to do when it breaks."""
    gaps = []
    for promise in promises:
        if promise.get("state", WAITING) != WAITING:
            continue
        if not promise.get("next_check") or not (promise.get("fallback") or "").strip():
            gaps.append(promise["promise_id"])
    return gaps
