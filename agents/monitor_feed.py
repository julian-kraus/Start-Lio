"""Show on the Lookahead screen what the monitor learns between two breaks.

Without this the screen stays still from the goal to the first broken promise, although an offer read from mail has
reached plan.offers and a supplier's check-in is a watched promise. Each tick, every offer read from mail and every
promise state the screen has not seen yet becomes records in the Lookahead log: an `event` for an offer, a `promise`
record plus an `event` for a watched or kept promise. A broken promise is left to the monitor's re-plan, which
publishes its own records: promise records are upserts, so once that break is published, and for a cancelled promise,
nothing is written here that would put it back to waiting.

Once per item: a `screen_fed` row in plan.timeline_events marks what was shown, in the same transaction that is
committed after the append, so a restart does not show it again. Names come from the supplier records already in the
log (written from kb at reset); without one the kb supplier id is shown. Nothing here names a supplier or part.
"""
from __future__ import annotations

from pathlib import Path

from zoneinfo import ZoneInfo

from psycopg.types.json import Json

from plan import clock
from tracker.lookahead import append_records

BERLIN = ZoneInfo("Europe/Berlin")
FED = "screen_fed"
# the event types the Overview story and the timeline already show
OFFER_RECEIVED = "offer_in"
PROMISE_WATCHED = PROMISE_KEPT = "check_in"


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _local(value):
    return value.astimezone(BERLIN).strftime("%a %H:%M") if hasattr(value, "astimezone") else value


def _amount(value) -> str | None:
    if value is None:
        return None
    number = float(value)
    return f"{number:,.0f}" if number == int(number) else f"{number:,.2f}"


def _source(sources):
    return sources[-1] if sources else None


def offer_records(offer: dict, name: str, now) -> list[dict]:
    qty, price = _amount(offer.get("quantity")), _amount(offer.get("unit_price_eur"))
    terms = " ".join(t for t in (f"{qty} x {offer['part_id']}" if qty else str(offer["part_id"]),
                                 f"at {price} EUR" if price else "") if t)
    title = f"Offer received from {name}: {terms}"
    return [{"kind": "event", "type": OFFER_RECEIVED, "severity": "info", "title": title, "at": _iso(now),
             "supplier_id": offer["supplier_id"], "lane": offer["part_id"], "offer_id": offer["offer_id"],
             "source": _source(offer.get("sources") or []), "detail": {"happened": title}}]


def promise_records(promise: dict, state: str, name: str, now) -> list[dict]:
    lane = promise.get("lane") or promise.get("part_id")
    shared = {"supplier_id": promise["supplier_id"], **({"lane": lane} if lane else {})}
    record = {**shared, "kind": "promise", "promise_id": promise["promise_id"], "state": state,
              "what": promise["what"], "due": _iso(promise["due"]), "sources": promise.get("sources") or []}
    # the screen says from these what happens if the proof is missing, and when the monitor looks
    record.update({k: _iso(promise[k]) for k in ("fallback", "proof", "next_check") if promise.get(k)})
    if state == "proven" and promise.get("proven_at"):
        record["proven_at"] = _iso(promise["proven_at"])
    if state == "waiting":
        title = f"Watching {name}: {promise['what']}, due {_local(promise['due'])}"
        kind = PROMISE_WATCHED
    else:
        title = f"{name} kept its promise: {promise['what']}"
        kind = PROMISE_KEPT
    event = {**shared, "kind": "event", "type": kind, "severity": "info", "title": title, "at": _iso(now),
             "promise_id": promise["promise_id"], "source": _source(record["sources"]),
             "detail": {"happened": title}}
    return [record, event]


def pending(plan_conn, names: dict, now, handled: str) -> list[tuple[str, str | None, list[dict]]]:
    """(key, supplier_id, records) for each offer and promise state the screen has not been shown yet. `handled` is
    the timeline event type that marks a break the re-plan has published."""
    fed = {r["k"] for r in plan_conn.execute(
        "SELECT payload->>'key' AS k FROM timeline_events WHERE type = %s", (FED,))}
    out = []
    for offer in plan_conn.execute("SELECT * FROM offers WHERE offer_id LIKE 'kb:%%' ORDER BY offer_id"):
        key = f"offer:{offer['offer_id']}"
        if key not in fed:
            name = names.get(offer["supplier_id"]) or offer["supplier_id"]
            out.append((key, offer["supplier_id"], offer_records(offer, name, now)))
    for promise in plan_conn.execute(
            "SELECT * FROM promises p WHERE state IN ('waiting', 'proven') OR (state = 'broken' AND NOT EXISTS ("
            "SELECT 1 FROM timeline_events e WHERE e.type = %s AND e.payload->>'promise_id' = p.promise_id)) "
            "ORDER BY created_at, promise_id", (handled,)):
        name = names.get(promise["supplier_id"]) or promise["supplier_id"]
        # a promise is shown as watched first, even if it broke or was kept in the same tick
        states = ["waiting"] + (["proven"] if promise["state"] == "proven" else [])
        for state in states:
            key = f"promise:{promise['promise_id']}:{state}"
            if key not in fed:
                out.append((key, promise["supplier_id"], promise_records(promise, state, name, now)))
    return out


def feed(plan_conn, log_path: Path, names: dict, producer: str, handled: str) -> int:
    """Append what is pending, one item per transaction. Returns the number of items shown."""
    now = clock.now(plan_conn)
    shown = 0
    for key, supplier, records in pending(plan_conn, names, now, handled):
        plan_conn.execute("INSERT INTO timeline_events (at, type, title, severity, supplier_id, payload, scenario) "
                          "VALUES (%s, %s, %s, 'info', %s, %s, %s)",
                          (now, FED, key, supplier, Json({"key": key}), clock.is_scenario(plan_conn)))
        try:
            append_records(Path(log_path), records, producer=producer)
        except Exception:
            plan_conn.rollback()        # not marked, so the next tick shows it
            raise
        plan_conn.commit()
        shown += 1
    plan_conn.commit()
    return shown
