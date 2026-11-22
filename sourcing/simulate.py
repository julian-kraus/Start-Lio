"""What a list of actions would change in coverage. Pure: it orders, approves and sends nothing.

    result = simulate(facts, actions, now=clock.now(conn), deadline=goal_deadline)

An empty list gives exactly `coverage.compute`. Two kinds of action, both plain dicts:

  {"kind": "offer", "offer": <offer contract>, "quantity": optional part of the offer}
      lands at the packing site at `available_at`, else `lead_time_h` after now
  {"kind": "conditional_release", "part_id", "batch", "quantity", "available_at", "valid_until"?, "sources"}
      lets units held for failed inspection back in, at most what is held

Every action comes back in `actions` as feasible or with the reason it is not; only feasible ones count.
Added units follow the rule of inbound: what lands after the part ran out only helps in the pack hours left.
Priority is the policy's, so a supply can move copies from later editions to an earlier one and the total can fall,
also with equal use per copy: one copy of the earlier edition can displace one of each later edition.
`min_quantity` holds for what an action gets. Where takes of one offer compete, the ones that make most units usable
count, earlier first on a tie, so one more action never lowers copies in policy order.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from plan import clock, contracts

from . import coverage, coverage_stock, coverage_windows
from .facts import Facts, figure, ids, number

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


def packed_by_deadline(on_hand, arrivals: list[tuple], burn: float,
                       windows: list[coverage_windows.Window]) -> int:
    """Units of one part that can still be used. Pack time lost while the part was out does not come back."""
    total = on_hand + sum(qty for _, qty in arrivals)
    if not burn or not windows:
        return int(total)
    idle = _idle(on_hand, arrivals, burn, windows)
    if not idle:
        return int(total)
    return int(min(total, burn * coverage_windows.hours(windows) - idle) + 1e-9)      # 1e-9: float noise


def _idle(on_hand, arrivals: list[tuple], burn: float, windows: list[coverage_windows.Window]) -> float:
    """What could have been packed while the part was out, up to its last arrival."""
    have, idle, by_time = on_hand, 0, defaultdict(int)
    for at, qty in arrivals:
        by_time[at] += qty
    for at in sorted(by_time):
        idle = max(idle, burn * coverage_windows.hours(windows, until=at) - have)
        have += by_time[at]
    return idle


def _best_takes(supplies: list[Supply], on_hand, arrivals: list[tuple], burn: float,
                windows: list[coverage_windows.Window]) -> tuple[list[tuple], Counter]:
    """(supply, units, units that count) for one part, and what that takes from each pool.

    Earliest first, so one more action can never push a pool's units to a later hour. A minimum breaks that, as an
    earlier take can clip a later one to nothing. Leaving a take out is then tried too, and the most usable wins.
    Ways that leave the same pools, count and idle time are one way: no later take can tell them apart.
    """
    ways = [([], Counter(), [], packed_by_deadline(on_hand, arrivals, burn, windows), 0)]
    for n, s in enumerate(supplies):
        rivals = [r for r in supplies[n:] if r.pool == s.pool]
        grown: dict = {}
        for takes, taken, landed, packed, counted in ways:
            units = min(s.quantity, max(0, s.limit - taken[s.pool]))
            options = [units] if units and units >= s.minimum else [0]
            after = taken[s.pool] + units + sum(r.quantity for r in rivals[1:])
            if options[0] and any(r.minimum and after > r.limit for r in rivals[1:]):
                options.append(0)                       # taking it may cost a later take its minimum
            for got in options:
                now_landed, now_taken, now_packed, counts = landed, taken, packed, 0
                if got:
                    now_landed, now_taken = [*landed, (s.at, got)], taken + Counter({s.pool: got})
                    now_packed = packed_by_deadline(on_hand, [*arrivals, *now_landed], burn, windows)
                    counts = min(got, max(0, now_packed - packed))
                way = ([*takes, (s, got, counts)], now_taken, now_landed, now_packed, counted + counts)
                so_far = [*(a for a in arrivals if a[0] <= s.at), *now_landed]
                idle = _idle(on_hand, so_far, burn, windows) if burn and windows else 0
                grown.setdefault((frozenset(now_taken.items()), way[4], idle), way)
        ways = list(grown.values())
    takes, taken, *_ = max(ways, key=lambda way: (way[4], sum(way[1].values())))
    return takes, taken


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
    by_part, chosen, taken = defaultdict(list), [], Counter()
    for s in sorted(supplies, key=lambda s: (s.at, s.index)):
        by_part[s.material].append(s)
    for material, offered in by_part.items():
        burn = sum((e.rate or 0) * boms[e.product_id][material][0] for e in users[material] if e.open)
        takes, used = _best_takes(offered, parts[material]["usable"]["value"], arrivals[material], burn, windows)
        chosen += takes
        taken += used

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

    order, policy_ids = plan["priority"]["order"], plan["priority"]["fact_ids"]
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
        wants = {e.product_id: e.open * boms[e.product_id][material][0] for e in users[material]}
        shares = coverage.share(have, wants, order)
        for row in part["allocation"]:
            row["allocated"] = {**figure(shares[row["product_id"]], need_ids, have_ids, policy_ids),
                                "inputs": inputs}

    available = {m: p["available"]["value"] for m, p in parts.items()}
    allocated = {m: {a["product_id"]: a["allocated"]["value"] for a in p["allocation"]} for m, p in parts.items()}
    packed = coverage.packable(eds, boms, available, order, allocated)
    used_ids = ids(*(parts[m]["added"]["fact_ids"] for m in added))
    inputs = _unique(i for m in added for i in parts[m]["added"]["inputs"])
    for e, row in zip(eds, plan["editions"]):
        copies, limited_by = packed[e.product_id]
        sources = ids(row["covered"]["fact_ids"], used_ids)
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


def _unique(items) -> list:
    out: list = []
    for item in items:
        if item not in out:
            out.append(item)
    return out
