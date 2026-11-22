"""What a list of actions would change in coverage. Pure: it orders, approves and sends nothing.

    result = simulate(facts, actions, now=clock.now(conn), deadline=goal_deadline)

An empty list gives exactly `coverage.compute`. Two kinds of action, both plain dicts:

  {"kind": "offer", "offer": <offer contract>, "quantity": optional part of the offer}
      lands at the packing site at `available_at`, else `lead_time_h` after now
  {"kind": "conditional_release", "part_id", "batch", "quantity", "available_at", "valid_until"?, "sources"}
      lets units held for failed inspection back in, at most what is held

Every action comes back in `actions` as feasible or with the reason it is not; only feasible ones count.
Added units follow the rule of inbound: what lands after the part ran out only helps in the pack hours left, and
each line gets no more of a shared part than it can still pack.
Priority is the policy's, so a supply can move copies from later editions to an earlier one and the total can fall,
also with equal use per copy: one copy of the earlier edition can displace one of each later edition.
`min_quantity` holds for what an action gets. Where takes of one offer compete, the ones that pack most copies in
policy order count (most copies in all without a policy), so one more action never lowers copies in policy order.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import product as combinations_of

from plan import clock, contracts

from . import coverage, coverage_stock, coverage_windows
from .facts import Facts, figure, ids, number
from .simulate_takes import Way, ways_to_take

CLOSED = ("declined", "expired")


class Infeasible(Exception):
    """Why one action cannot count."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


@dataclass
class Supply:
    index: int
    kind: str
    material: str
    quantity: int
    limit: int                  # the most its pool can give, however many actions draw on it
    pool: tuple
    at: datetime
    inputs: list
    evidence: list = field(default_factory=list)
    minimum: float = 0          # the least one action may take


def _instant(value, name: str) -> datetime:
    try:
        return clock.parse(value)
    except (clock.ClockError, TypeError, ValueError, AttributeError) as exc:
        raise Infeasible("invalid_action", f"{name} needs a time with a UTC offset") from exc


def _units(value) -> int:
    try:
        count = number(value) if isinstance(value, (int, float)) else None
    except OverflowError:
        count = None
    if count is None or not 1 <= count < math.inf:
        raise Infeasible("invalid_action", "quantity must be a number of at least 1")
    return int(count)


def _input_ids(inputs: list) -> list[str]:
    return ids(str(s["fact_id"]) for s in inputs if isinstance(s, dict) and s.get("fact_id"))


def _check_sources(facts: Facts, inputs) -> None:
    probe = {"covered": 0, "needed": 0, "unit": "copies", "sources": inputs}
    if not isinstance(inputs, list) or not inputs or contracts.errors("coverage", probe):
        raise Infeasible("invalid_action", "sources must name where the numbers come from")
    if unknown := [i for i in _input_ids(inputs) if i not in facts.by_id]:
        raise Infeasible("unknown_fact", ", ".join(unknown))


def _check_validity(valid_until, now: datetime) -> None:
    if valid_until is not None and _instant(valid_until, "valid_until") <= now:
        raise Infeasible("expired")


def _part(facts: Facts, name, parts: dict) -> dict:
    material = facts.material(str(name or ""))
    if material not in parts:
        raise Infeasible("part_not_in_plan", material)
    return parts[material]


def _landing(at: datetime, now: datetime, deadline: datetime) -> datetime:
    at = max(at, now)                                   # nothing decided now can have landed earlier
    if at >= deadline:
        raise Infeasible("after_deadline")
    return at


def _offer(facts: Facts, index: int, action: dict, parts: dict, revisions: dict,
           now: datetime, deadline: datetime) -> Supply:
    offer = action.get("offer")
    if problems := contracts.errors("offer", offer):
        raise Infeasible("invalid_action", "; ".join(problems))
    _check_sources(facts, offer["sources"])
    if offer.get("status") in CLOSED:
        raise Infeasible(offer["status"])
    _check_validity(offer.get("valid_until"), now)
    part = _part(facts, offer["part_id"], parts)
    if "quantity" not in offer:
        raise Infeasible("qty_unknown")
    limit = _units(offer["quantity"])
    wanted = _units(action.get("quantity", limit))
    if wanted > limit:
        raise Infeasible("more_than_offered")
    if wanted < offer.get("min_quantity", 0):
        raise Infeasible("below_min_quantity")
    if any(e["reference"] == offer["offer_id"] and e["counts"]["value"] for e in part["inbound"]):
        raise Infeasible("already_inbound")

    if "available_at" in offer:
        at = _instant(offer["available_at"], "available_at")
    elif "lead_time_h" in offer:
        try:
            at = now + timedelta(hours=offer["lead_time_h"])
        except OverflowError as exc:
            raise Infeasible("after_deadline") from exc
    else:
        raise Infeasible("arrival_unknown")
    at = _landing(at, now, deadline)

    evidence = []
    if dispute := revisions.get(part["material_id"]):
        ok, evidence = dispute.check([], offer["supplier_id"])
        if not ok:
            raise Infeasible("disputed_revision")
    return Supply(index, "offer", part["material_id"], wanted, limit,
                  ("offer", part["material_id"], offer["supplier_id"], offer["offer_id"]), at,
                  offer["sources"], evidence, offer.get("min_quantity", 0))


def _release(facts: Facts, index: int, action: dict, parts: dict, revisions: dict,
             now: datetime, deadline: datetime) -> Supply:
    inputs = action.get("sources")
    _check_sources(facts, inputs)
    _check_validity(action.get("valid_until"), now)
    part = _part(facts, action.get("part_id"), parts)
    material, batch = part["material_id"], str(action.get("batch") or "")
    held = [h["qty"] for h in part["holds"] if h["reason"] == "failed_inspection" and h.get("batch") == batch]
    if not held:
        raise Infeasible("nothing_to_release")
    limit = int(sum(q["value"] for q in held))
    wanted = _units(action.get("quantity"))
    if wanted > limit:
        raise Infeasible("more_than_held")
    evidence = ids(*(q["fact_ids"] for q in held))

    # a concession waives the inspection, not a disputed receipt and not a disputed revision
    receipt = next(iter(facts.rows("goods_receipt", gr_number=batch, material_id=material)), None)
    note = receipt.key("delivery_note") if receipt else ""
    if note and any(b.get("contradiction")
                    for b in facts.believed("received_qty", material_id=material, delivery_note=note)):
        raise Infeasible("disputed_receipt")
    if dispute := revisions.get(material):
        ok, found = dispute.check([receipt.key("po_number")] if receipt else [],
                                  receipt.key("supplier_id") if receipt else "", note)
        if not ok:
            raise Infeasible("disputed_revision")
        evidence = ids(evidence, found)
    if action.get("available_at") is None:
        raise Infeasible("arrival_unknown")
    at = _landing(_instant(action["available_at"], "available_at"), now, deadline)
    return Supply(index, "conditional_release", material, wanted, limit,
                  ("conditional_release", material, batch), at, inputs, evidence)


KINDS = {"offer": _offer, "conditional_release": _release}


def simulate(facts: Facts, actions: list[dict], *, now: datetime, deadline: datetime,
             products: list[str] | None = None) -> dict:
    """Coverage as it would be with the feasible actions taken now.

    As in coverage, `products` only narrows the report: an action works on the whole plan.
    """
    result = coverage.compute(facts, now=now, deadline=deadline, products=products)
    if not actions:
        return result

    plan = result if products is None else coverage.compute(facts, now=now, deadline=deadline)
    eds, _ = coverage.editions(facts)
    today = now.astimezone(deadline.tzinfo).date()
    phantom = {n["material_id"] for n in plan["not_shortages"]}
    boms = {e.product_id: {m: v for m, v in coverage.bill_of_materials(facts, e.product_id, today).items()
                           if m not in phantom} for e in eds}
    windows = coverage_windows.pack_windows(facts, now, deadline)
    parts = {p["material_id"]: p for p in plan["parts"]}
    revisions = coverage_stock.revision_disputes(facts, set(parts))

    supplies, report = [], []
    for index, action in enumerate(actions):
        kind = action.get("kind") if isinstance(action, dict) else None
        entry = {"index": index, "kind": kind if isinstance(kind, str) else None}
        report.append(entry)
        try:
            check = KINDS.get(kind) if isinstance(kind, str) else None
            if not check:
                raise Infeasible("invalid_action", f"kind must be one of {', '.join(KINDS)}")
            supplies.append(check(facts, index, action, parts, revisions, now, deadline))
        except Infeasible as no:
            entry.update(feasible=False, reason=no.reason)
            if no.detail:
                entry["detail"] = no.detail

    _count(plan, result, supplies, report, eds, boms, windows, deadline)
    result["actions"] = report
    return result


def _count(plan: dict, result: dict, supplies: list[Supply], report: list[dict], eds: list, boms: dict,
           windows: list, deadline: datetime) -> None:
    """Add the supplies to the parts they feed, pack the whole plan again, then report what was asked for."""
    parts = {p["material_id"]: p for p in plan["parts"]}
    users = {m: [e for e in eds if m in boms[e.product_id]] for m in parts}
    shift_ids = ids(*(w.fact_ids for w in windows))
    arrivals = {m: [(datetime.fromisoformat(i["arrives_at"]), i["counts"]["value"])
                    for i in p["inbound"] if i["counts"]["value"]] for m, p in parts.items()}
    by_part, options = defaultdict(list), {}
    for s in sorted(supplies, key=lambda s: (s.at, s.index)):
        by_part[s.material].append(s)
    for material, offered in by_part.items():
        burn = sum((e.rate or 0) * boms[e.product_id][material][0] for e in users[material] if e.open)
        options[material] = ways_to_take(offered, parts[material]["usable"]["value"], arrivals[material], burn, windows)

    order, policy_ids = plan["priority"]["order"], plan["priority"]["fact_ids"]
    rates = {m: {e.product_id: (e.rate or 0) * boms[e.product_id][m][0] for e in users[m]} for m in parts}
    wants = {m: {e.product_id: e.open * boms[e.product_id][m][0] for e in users[m]} for m in parts}

    def pack(ways: dict[str, Way]) -> tuple[dict, dict]:
        lots = {m: [(None, p["usable"]["value"]),
                    *sorted([*arrivals[m], *(ways[m].counted() if m in ways else [])])] for m, p in parts.items()}
        allocated = {m: coverage.share(lots[m], wants[m], order, rates[m], windows) for m in parts}
        return allocated, coverage.packable(eds, boms, lots, order, allocated, windows)

    ways = _best_ways(options, order, pack)
    chosen = [take for way in ways.values() for take in way.takes]
    taken = sum((way.taken for way in ways.values()), Counter())

    added = defaultdict(list)
    for s, units, counts in chosen:
        part, entry, using = parts[s.material], report[s.index], users[s.material]
        if not units:
            reason = "already_used" if taken[s.pool] >= s.limit else "below_min_quantity"
            entry.update(feasible=False, reason=reason)
            continue

        given = ids(_input_ids(s.inputs), s.evidence)
        counted_ids = ids(given, part["available"]["fact_ids"], shift_ids,
                          *(e.rate_ids for e in using), *(boms[e.product_id][s.material][1] for e in using))
        entry.update(feasible=True, part_id=s.material, arrives_at=s.at.astimezone(deadline.tzinfo).isoformat(),
                     quantity={**figure(units, given), "inputs": s.inputs},
                     counts={**figure(counts, counted_ids), "inputs": s.inputs})
        if counts < units:
            entry["limited_by"] = "pack_hour"
        if counts:
            added[s.material].append(entry["counts"])
    if not added:
        return

    allocated, packed = pack(ways)
    for material, counted in added.items():
        part = parts[material]
        inputs = _unique(i for c in counted for i in c["inputs"])
        more = sum(c["value"] for c in counted)
        more_ids = ids(*(c["fact_ids"] for c in counted))
        have = part["available"]["value"] + more
        have_ids = ids(part["available"]["fact_ids"], more_ids)
        need, need_ids = part["need"]["value"], part["need"]["fact_ids"]
        part["added"] = {**figure(more, more_ids), "inputs": inputs}
        part["available"] = {**figure(have, have_ids), "inputs": inputs}
        part["gap"] = {**figure(max(0, need - have), need_ids, have_ids), "inputs": inputs}
        split_ids = ids(policy_ids, shift_ids, *(e.rate_ids for e in users[material]))
        for row in part["allocation"]:
            row["allocated"] = {**figure(allocated[material][row["product_id"]], need_ids, have_ids, split_ids),
                                "inputs": inputs}

    used_ids = ids(*(parts[m]["added"]["fact_ids"] for m in added))
    inputs = _unique(i for m in added for i in parts[m]["added"]["inputs"])
    for e, row in zip(eds, plan["editions"]):
        copies, limited_by = packed[e.product_id]
        sources = ids(row["covered"]["fact_ids"], used_ids)
        if any(arrivals[m] or m in added for m in limited_by):
            sources = ids(sources, e.rate_ids, shift_ids)   # what arrives late is limited by the line's hours
        row.update(covered={**figure(copies, sources), "inputs": inputs},
                   gap={**figure(e.open - copies, sources), "inputs": inputs}, limited_by=limited_by)

    by_id = {e["product_id"]: e for e in plan["editions"]}
    result["editions"] = [by_id[e["product_id"]] for e in result["editions"]]
    result["parts"] = [parts[p["material_id"]] for p in result["parts"]]
    covered = sum(e["covered"]["value"] for e in result["editions"])
    top = result["fact_ids"]
    top["covered"], top["gap"] = ids(top["covered"], used_ids), ids(top["gap"], used_ids)
    result.update(covered=covered, gap=result["needed"] - covered,
                  sources=_unique([*({"fact_id": i} for i in top["gap"]), *inputs]))


def _best_ways(options: dict[str, list[Way]], order: list[str] | None, pack) -> dict[str, Way]:
    """One way per part, chosen over all parts at once, as copies need every part of a bill of materials."""
    every = [dict(zip(options, picked)) for picked in combinations_of(*options.values())]
    if len(every) == 1:
        return every[0]

    def worth(ways: dict[str, Way]) -> tuple:
        packed = pack(ways)[1]
        copies = [packed[p][0] for p in order] if order else [sum(n for n, _ in packed.values())]
        return copies, sum(q for way in ways.values() for _, q in way.counted())

    return max(every, key=worth)


def _unique(items) -> list:
    out: list = []
    for item in items:
        if item not in out:
            out.append(item)
    return out
