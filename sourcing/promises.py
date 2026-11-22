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

# How a supplier_statement kind (a kb vocabulary, not Soony data) moves an open promise. A kind not listed is
# informational and leaves the promise waiting. Callers may pass their own map to `reconcile`.
OUTCOMES: dict[str, str] = {
    "dispatch_info": PROVEN,
    "on_hold": BROKEN,
    "short_shipment": BROKEN,
    "term_rejected": BROKEN,
}

_MATCH_KEYS = ("supplier_id", "part_id", "lane")


class PromiseError(ValueError):
    pass


@dataclass(frozen=True)
class Statement:
    """A supplier statement as the kb recorded it (fact_type `supplier_statement`), normalized for the ledger.
    `kind` is the statement kind; `at` is when the supplier said it; `fact_id` cites the kb fact it came from."""
    kind: str
    at: str
    supplier_id: str | None = None
    part_id: str | None = None
    lane: str | None = None
    text: str | None = None
    fact_id: str | int | None = None

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


def _reference_time(promise: dict):
    """The moment from which a statement can bear on a promise: when it was made, else created, else its due."""
    for key in ("made_at", "created_at", "due"):
        value = promise.get(key)
        if value:
            return timing.instant(value)
    return None


def reconcile(promises: Iterable[dict], statements: Iterable[Statement], *,
              outcomes: dict[str, str] = OUTCOMES) -> list[dict]:
    """For each waiting promise, the state change the supplier statements force, or nothing. The earliest
    decisive statement (proven or broken) at or after the promise was made wins; informational statements and
    statements for a different supplier, part or lane are ignored. Deterministic and side-effect free."""
    ordered = sorted(statements, key=lambda s: timing.instant(s.at))
    updates: list[dict] = []
    for promise in promises:
        if promise.get("state", WAITING) != WAITING:
            continue
        since = _reference_time(promise)
        for st in ordered:
            outcome = outcomes.get(st.kind)
            if outcome is None or not _matches(promise, st):
                continue
            if since is not None and timing.instant(st.at) < since:
                continue
            at_key = "proven_at" if outcome == PROVEN else "broken_at"
            updates.append({"promise_id": promise["promise_id"], "state": outcome, at_key: st.at,
                            "source": st.source(), "statement_kind": st.kind})
            break
    return updates


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
