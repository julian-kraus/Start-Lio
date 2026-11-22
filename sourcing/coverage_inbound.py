"""Inbound that can still be packed: ETA plus last-mile transit against the hour the part runs out.

Nothing undelivered arrives before now, and what still waits at the port then has its road leg ahead. Once the part has run out, later arrivals share the pack hours that
remain, so splitting a shipment does not change what counts.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, time, timedelta

from .coverage_stock import BLOCKED, Revision
from .coverage_windows import Window, hours
from .facts import Facts, believed_ids, day, figure, id_list, ids, number

GONE = ("cancelled", "canceled")
LANDED = ("delivered", "received")
REFUSED = ("refused", "rejected", "denied")
AWAITS_PICKUP = re.compile(r"(^|_)(port|airport|terminal|customs|pick_?up)(_|$)")


def _status(value) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def shipments(facts: Facts) -> dict[str, dict]:
    """One entry per transport reference, merged from every list that names it."""
    grouped: dict[str, dict] = defaultdict(lambda: {"rows": [], "lines": []})
    for row in facts.rows("shipment"):
        if ref := row.key("reference") or row.key("shipment_id"):
            grouped[ref]["rows"].append(row)
    for row in facts.rows("shipment_qty"):
        if ref := row.key("reference"):
            grouped[ref]["lines"].append(row)
    out = {}
    for ref, group in sorted(grouped.items()):
        # the fullest record speaks for the shipment, plan lines fill what it leaves open
        rows = sorted(group["rows"], key=lambda r: -len(r.values)) + group["lines"]
        ship = {"reference": ref, "rows": rows, "lines": defaultdict(list), "ids": {}}
        for name in ("shipment_id", "destination", "dest_site", "mode", "supplier_id", "status", "dock_slot"):
            row = next((r for r in rows if r.key(name)), None)
            ship[name] = row.key(name) if row else ""
            ship["ids"][name] = row.ids(name) if row else []
        for b in facts.believed("shipment_status", shipment_id=ship["shipment_id"]):
            if b.get("value") is not None:
                ship["status"], ship["ids"]["status"] = b["value"], believed_ids(b)
        etas = [(d, r.ids("eta")) for r in rows if (d := day(r.get("eta")))]
        ship["eta"], ship["ids"]["eta"] = max(etas, key=lambda e: e[0]) if etas else (None, [])
        ship["pos"] = sorted({p for r in rows for name in ("po_numbers", "po_number") for p in id_list(r.get(name))})
        for line in group["lines"]:
            ship["lines"][facts.material(line.key("material_id"))].append(line)
        ship["materials"] = set(ship["lines"]) | {facts.material(m) for r in group["rows"]
                                                  for m in id_list(r.get("material_ids"))}
        out[ref] = ship
    return out


def quantity(facts: Facts, ship: dict, material: str) -> tuple[int | float | None, list[str]]:
    lines = ship["lines"].get(material, [])
    if len(ship["lines"]) == 1 and lines:               # the kb rule is keyed by reference alone
        for b in facts.believed("shipment_qty", reference=ship["reference"]):
            if (n := number(b.get("value"))) is not None:
                return n, believed_ids(b)
            if b.get("contradiction"):
                return None, believed_ids(b)
    for name in ("qty_shipped", "qty_planned"):
        found = [(n, r.ids(name)) for r in lines if (n := number(r.get(name))) is not None]
        if found:
            return min(found, key=lambda f: f[0])
    return None, ids(*(r.ids("material_ids", "reference") for r in ship["rows"]))


def last_mile(facts: Facts, ship: dict, sites: set[str]) -> tuple[int | float | None, list[str]]:
    destination = ship["destination"]
    if destination in sites:
        return 0, ship["ids"]["destination"]
    targets = {ship["dest_site"]} if ship["dest_site"] else sites
    legs = [(n, r.ids("transit_days")) for r in facts.rows("route", origin=destination, mode="road")
            if r.key("destination") in targets and (n := number(r.get("transit_days"))) is not None]
    return min(legs, key=lambda leg: leg[0]) if destination and legs else (None, ship["ids"]["destination"])


def dock_rule(facts: Facts) -> list[str]:
    return ids(*(r.ids("directive") for r in facts.rows("policy_directive")
                 if "dock slot" in r.key("directive").lower()))


def held_orders(facts: Facts) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for row in facts.rows("supplier_statement", kind="on_hold"):
        for name in ("po_number", "reference"):
            if row.key(name):
                out[row.key(name)] += row.ids("kind", "value")
    return dict(out)


def arrival(ship: dict, transit, windows: list[Window], now: datetime, deadline: datetime) -> datetime:
    """When it is at the packing site: that day's first shift, as facts carry dates, but never before now."""
    here = now.astimezone(deadline.tzinfo).date()
    leg = timedelta(days=math.ceil(transit))            # facts carry dates: part of a day is a day

    def first_shift(due) -> datetime:
        starts = [w.start for w in windows if w.start.date() == due]
        return min(starts, default=datetime.combine(due, time(), deadline.tzinfo))

    due = ship["eta"] + leg
    if due < here:
        due = here + leg                                # overdue or waiting for pick-up: the leg is still ahead
    at = first_shift(due)
    if at < now and AWAITS_PICKUP.search(_status(ship["status"])):
        at = first_shift(here + leg)                    # it should be here by now and still waits for pick-up
    return max(at, now.astimezone(deadline.tzinfo))


def inbound_position(facts: Facts, material: str, ships: dict[str, dict], windows: list[Window],
                     now: datetime, deadline: datetime, sites: set[str], usable: dict, burn: float,
                     burn_ids: list[str], revision: Revision | None) -> tuple[list[dict], list[dict]]:
    """Every shipment of one part with the units that count and, where none do, why."""
    dock, holds = dock_rule(facts), held_orders(facts)
    shift_ids = ids(*(w.fact_ids for w in windows))
    found = []
    for ship in ships.values():
        if material in ship["materials"]:
            days, day_ids = last_mile(facts, ship, sites)
            at = None
            if ship["eta"] and days is not None:
                at = arrival(ship, days, windows, now, deadline)
            found.append((ship, days, day_ids, at))
    found.sort(key=lambda f: (f[3] is None, f[3] or deadline, f[0]["reference"]))

    entries, alerts = [], []
    on_hand = usable["value"]           # usable stock plus what counted so far
    idle = 0.0                          # units the lines could not pack because the part had run out
    for ship, days, day_ids, at in found:
        qty, qty_ids = quantity(facts, ship, material)
        status = _status(ship["status"])
        sources = ids(qty_ids, ship["ids"]["eta"], day_ids, ship["ids"]["status"])
        entry = {"reference": ship["reference"], "status": status}
        if at:
            entry.update(eta=ship["eta"].isoformat(), arrives_at=at.isoformat(),
                         last_mile_days=figure(days, day_ids))
        if qty is not None:
            entry["qty"] = figure(qty, qty_ids)
        reason, count = None, 0
        before_deadline = at is not None and at < deadline
        if status in GONE:
            reason = "cancelled"
        elif status in LANDED:
            reason = "already_received"
        elif held := ids(*(holds.get(k, []) for k in [ship["reference"], *ship["pos"]])):
            reason, sources = "on_hold", ids(sources, held)
        elif not ship["eta"]:
            reason = "eta_unknown"
        elif days is None:
            reason = "last_mile_unknown"
        elif not before_deadline:
            reason = "after_deadline"
        elif qty is None:
            reason = "qty_unknown"
        elif _status(ship["dock_slot"]) in REFUSED:
            reason, sources = "dock_slot_refused", ids(sources, ship["ids"]["dock_slot"])
        else:
            if revision:
                ok, evidence = revision.check(ship["pos"], ship["supplier_id"])
                sources = ids(sources, evidence)
                if not ok:
                    reason = "disputed_revision"
            if not reason:
                count = qty
                if burn and windows:
                    room, left = hours(windows, since=at) * burn, on_hand + idle - hours(windows, until=at) * burn
                    if left < 0:
                        idle, left = idle - left, 0
                    if not room:
                        count = 0
                    elif idle:
                        # the part ran out: only what the remaining hours can take beyond what is already there
                        count = min(qty, max(0, int(round(room - left, 6))))
                    if idle or not room:
                        sources = ids(sources, usable["fact_ids"], *(e["counts"]["fact_ids"] for e in entries
                                                                    if e["counts"]["value"]), burn_ids, shift_ids)
                    if count == 0:
                        reason = "after_pack_hour"
                    elif count < qty:
                        entry["limited_by"] = "pack_hour"
        entry["counts"] = figure(count, sources)
        if reason:
            entry["not_counted"] = reason
            if reason == "disputed_revision":
                entry["state"] = BLOCKED
            if reason in ("on_hold", "eta_unknown", "last_mile_unknown", "qty_unknown", "dock_slot_refused",
                          "disputed_revision"):
                alerts.append({"kind": f"inbound_{reason}", "material_id": material,
                               "reference": ship["reference"], "fact_ids": sources})
        elif dock:
            released = _status(ship["dock_slot"]) == "released"
            entry["dock_slot"] = {"required": True, "state": "released" if released else "unknown",
                                  "fact_ids": ids(dock, ship["ids"]["dock_slot"])}
            if not released:
                alerts.append({"kind": "dock_slot_release", "material_id": material,
                               "reference": ship["reference"], "fact_ids": ids(dock, sources)})
        on_hand += count
        entries.append(entry)
    return entries, alerts
