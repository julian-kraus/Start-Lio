"""Coverage: how many open copies the believed stock and the in-time inbound can still pack.

    result = compute(facts.load(conn), now=clock.now(conn), deadline=goal_deadline)

Pure: same facts, clock and deadline give the same result, which validates against the coverage contract.
Every number is a figure with fact_ids; covered, needed and gap are plain numbers with theirs in `fact_ids`.

  need        launch plan demand minus the pack-out log, times BOM qty_per
  usable      see coverage_stock; safety stock is reported and is never a launch buffer
  inbound     see coverage_inbound; counted units may still need a dock slot release
  allocated   a shared part reserved in policy order, up to each edition's full need
  covered     copies that can be packed, editions in policy order; what one cannot use goes to the next
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from . import coverage_inbound, coverage_stock, coverage_windows
from .facts import Facts, believed_ids, day, figure, ids, number

PHANTOM = re.compile(r"\bphantom\b|\bfitted by (the )?supplier\b|\b(not|never)\b.*\bstocked separately\b", re.I)
PRIORITY = re.compile(r"\bpriority\b.*\bfirst\b", re.I | re.S)
PRIORITY_SOURCES = (("policy_directive", "directive"), ("x_workbook_row", "note"), ("x_document_line", "text"))
OUTSIDE = ("suppliers/", "mail/")                       # a supplier cannot set our priority


@dataclass
class Edition:
    product_id: str
    name: str
    demand: int
    demand_ids: list
    packed: int
    open: int
    open_ids: list
    reported_packed: int | None
    reported_ids: list
    rate: float | None
    rate_ids: list
    line: str
    line_status: str


def editions(facts: Facts, products: list[str] | None = None) -> tuple[list[Edition], list[dict]]:
    grouped: dict[str, list] = {}
    for row in facts.rows("plan_edition"):
        if products is None or row.key("product_id") in products:
            grouped.setdefault(row.key("product_id"), []).append(row)
    out, alerts = [], []
    for product, rows in sorted(grouped.items()):
        demands = [(n, r.ids("demand")) for r in rows if (n := number(r.get("demand"))) is not None]
        if not demands:
            continue
        demand, demand_ids = max(demands, key=lambda d: d[0])
        if len({n for n, _ in demands}) > 1:
            alerts.append({"kind": "demand_disputed", "product_id": product,
                           "fact_ids": ids(*(i for _, i in demands))})
        log = facts.rows("packout", product_id=product)
        packed = sum(number(r.get("units_packed")) or 0 for r in log)
        packed_ids = ids(*(r.ids("units_packed") for r in log))
        if not log:
            alerts.append({"kind": "packout_log_missing", "product_id": product, "fact_ids": ids(demand_ids)})
        reports = [(n, r.ids("packed")) for r in rows if (n := number(r.get("packed"))) is not None]
        reported, reported_ids = max(reports, key=lambda d: d[0]) if reports else (None, [])
        if reported is not None and reported != packed:
            alerts.append({"kind": "packed_differs_from_log", "product_id": product,
                           "fact_ids": ids(reported_ids, packed_ids)})
        rates = [(n, r.ids("rate_per_hour")) for r in rows if (n := number(r.get("rate_per_hour")))]
        rate, rate_ids = min(rates, key=lambda d: d[0]) if rates else (None, [])
        out.append(Edition(
            product, next((r.key(k) for k in ("edition_name", "name") for r in rows if r.key(k)), product),
            demand, demand_ids, packed, max(0, demand - packed), ids(demand_ids, packed_ids),
            reported, reported_ids, rate, rate_ids,
            next((r.key("po_line") for r in log if r.key("po_line")), product),
            next((r.key("line_status") for r in rows if r.key("line_status")), "unknown")))
    if products:
        missing = set(products) - {e.product_id for e in out}
        if missing:
            raise ValueError(f"no launch plan demand for {sorted(missing)}")
    return out, alerts


def bill_of_materials(facts: Facts, product: str, today) -> dict[str, tuple]:
    out = {}
    for row in facts.rows("bom_line", parent_id=product):
        starts, ends, qty = day(row.get("valid_from")), day(row.get("valid_to")), number(row.get("qty_per"))
        if qty and (not starts or starts <= today) and (not ends or today <= ends):
            out[row.key("child_id")] = (qty, row.ids("qty_per"))
    changes = facts.rows("change_record", parent_id=product)
    effective = {c.key("change_id"): (d, c.ids("effective_from")) for c in changes
                 if (d := day(c.get("effective_from")))}
    due = [(effective[c.key("change_id")], c) for c in changes
           if (c.key("field") or c.key("change_field")) == "qty_per" and c.key("change_id") in effective]
    for (starts, date_ids), change in sorted(due, key=lambda item: item[0][0]):
        child, qty = change.key("child_id"), number(change.get("new"))
        if starts <= today and child in out and qty is not None:
            out[child] = (qty, ids(out[child][1], change.ids("new"), date_ids))
    return out


def phantom_evidence(facts: Facts, material: str) -> list[str]:
    """Why a BOM line is no shortage, as kb has it. Empty when it is an ordinary part."""
    for b in facts.believed("phantom_part", material_id=material):
        return believed_ids(b) if b.get("value") else []
    for issue in facts.issues:
        payload = issue.get("payload") or {}
        key = payload.get("key") if isinstance(payload.get("key"), dict) else payload
        if issue.get("kind") == "phantom_part" and key.get("material_id") == material:
            return ids(issue.get("fact_ids") or [])
    if any((number(r.get("qty")) or 0) > 0 for r in facts.rows("stock_batch", material_id=material)):
        return []                                       # it is stocked, so it is no phantom
    return ids(*(r.ids("note") for kind in ("bom_line", "component", "supplier_part", "part_xref")
                 for r in facts.rows(kind)
                 if material in (r.key("material_id"), r.key("child_id")) and PHANTOM.search(r.key("note"))))


def priority(facts: Facts, eds: list[Edition]) -> tuple[list[str] | None, list[str]]:
    """(product ids in policy order, the policy's fact ids). None when no single policy says."""
    found = []
    for kind, attribute in PRIORITY_SOURCES:
        for row in facts.rows(kind):
            text = row.key(attribute)
            if row.path.startswith(OUTSIDE) or not PRIORITY.search(text):
                continue
            low, named = text.lower(), []
            for e in eds:
                hits = [m.start() for label in (e.product_id, e.name)
                        if (m := re.search(rf"(?<!\w){re.escape(label.lower())}(?!\w)", low))]
                if hits:
                    named.append((min(hits), e.product_id))
            if named:
                order = [p for _, p in sorted(named)]
                order += [e.product_id for e in eds if e.product_id not in order]
                found.append((row.as_of, order, row.ids(attribute)))
    if not found:
        return None, []
    newest = max(f[0] for f in found)
    latest = [f for f in found if f[0] == newest]
    sources = ids(*(f[2] for f in latest))
    return (latest[0][1] if len({tuple(f[1]) for f in latest}) == 1 else None), sources


def share(available, wants: dict[str, float], order: list[str] | None) -> dict[str, float]:
    """Split one part: in policy order, or in proportion to need when no policy decides."""
    if order is None:
        total = sum(wants.values())
        if total <= available:
            return dict(wants)
        return {p: int(available * want // total) for p, want in wants.items()}
    out, left = {}, available
    for product in (p for p in order if p in wants):
        out[product] = min(left, wants[product])
        left -= out[product]
    return out


def packable(eds: list[Edition], boms: dict, available: dict, order: list[str] | None,
             allocated: dict) -> dict[str, tuple[int, list[str]]]:
    """Copies per edition and the parts that limit it."""
    out, left = {}, dict(available)
    by_id = {e.product_id: e for e in eds}
    for product in order or by_id:
        edition, bom = by_id[product], boms[product]
        pool = left if order else {m: allocated[m][product] for m in bom}
        limits = {m: int(pool[m] // qty) for m, (qty, _) in bom.items()}
        copies = min([edition.open, *limits.values()]) if bom else 0
        out[product] = copies, sorted(m for m, n in limits.items() if n == copies and n < edition.open)
        for m, (qty, _) in bom.items():
            left[m] -= copies * qty
    return out


def compute(facts: Facts, *, now: datetime, deadline: datetime, products: list[str] | None = None) -> dict:
    if now.tzinfo is None or deadline.tzinfo is None:
        raise ValueError("now and deadline need a UTC offset")
    eds, alerts = editions(facts, products)
    if not eds:
        raise ValueError("no launch plan demand in the facts")
    today = now.astimezone(deadline.tzinfo).date()
    windows = coverage_windows.pack_windows(facts, now, deadline)

    all_boms = {e.product_id: bill_of_materials(facts, e.product_id, today) for e in eds}
    phantom = {m: found for m in sorted({m for bom in all_boms.values() for m in bom})
               if (found := phantom_evidence(facts, m))}
    boms = {p: {m: v for m, v in bom.items() if m not in phantom} for p, bom in all_boms.items()}
    for e in eds:
        if not all_boms[e.product_id]:
            alerts.append({"kind": "bom_missing", "product_id": e.product_id, "fact_ids": ids(e.open_ids)})

    order, policy_ids = priority(facts, eds)
    materials = sorted({m for bom in boms.values() for m in bom})
    if order is None and any(sum(m in bom for bom in boms.values()) > 1 for m in materials):
        alerts.append({"kind": "priority_unknown", "fact_ids": ids(policy_ids, *(e.demand_ids for e in eds))})

    revisions = coverage_stock.revision_disputes(facts, set(materials))
    ships = coverage_inbound.shipments(facts)
    sites = {r.key("site_id") for r in facts.rows("packout") if r.key("site_id")}
    sites = sites or {r.key("site_id") for r in facts.rows("site") if r.key("site_id")}

    parts, available, allocated, part_ids = [], {}, {}, {}
    for material in materials:
        users = [e for e in eds if material in boms[e.product_id]]
        wants = {e.product_id: e.open * boms[e.product_id][material][0] for e in users}
        bom_ids = ids(*(boms[e.product_id][material][1] for e in users))
        need_ids = ids(bom_ids, *(e.open_ids for e in users))
        need = sum(wants.values())
        stock, stock_alerts = coverage_stock.stock_position(facts, material, sites, revisions.get(material), bom_ids)
        burn = sum((e.rate or 0) * boms[e.product_id][material][0] for e in users if e.open)
        inbound, inbound_alerts = coverage_inbound.inbound_position(
            facts, material, ships, windows, now, deadline, sites, stock["usable"], burn,
            ids(bom_ids, *(e.rate_ids for e in users)), revisions.get(material))
        alerts += stock_alerts + inbound_alerts

        counted = [i["counts"] for i in inbound if i["counts"]["value"]]
        stock_ids = stock["usable"]["fact_ids"]
        have = stock["usable"]["value"] + sum(c["value"] for c in counted)
        have_ids = ids(stock_ids, *(c["fact_ids"] for c in counted))
        available[material] = have
        part_ids[material] = have_ids
        allocated[material] = share(have, wants, order)
        parts.append({
            "material_id": material, "need": figure(need, need_ids), **stock, "inbound": inbound,
            "inbound_counted": figure(sum(c["value"] for c in counted),
                                      *([c["fact_ids"] for c in counted] or [bom_ids])),
            "available": figure(have, have_ids),
            "gap": figure(max(0, need - have), need_ids, have_ids),
            "gap_on_stock": figure(max(0, need - stock["usable"]["value"]), need_ids, stock_ids),
            "allocation": [{"product_id": p, "need": figure(wants[p], need_ids),
                            "allocated": figure(n, need_ids, have_ids, policy_ids)}
                           for p, n in allocated[material].items()],
        })

    packed = packable(eds, boms, available, order, allocated)
    out_editions = []
    for e in eds:
        copies, limited_by = packed[e.product_id]
        bom = boms[e.product_id]
        sources = ids(e.open_ids, policy_ids, *(bom[m][1] for m in bom), *(part_ids[m] for m in bom))
        capacity, capacity_alerts = coverage_windows.capacity(facts, e, windows, now)
        alerts += capacity_alerts
        out_editions.append({
            "product_id": e.product_id, "name": e.name, "demand": figure(e.demand, e.demand_ids),
            "packed": figure(e.packed, e.open_ids), "open": figure(e.open, e.open_ids),
            "covered": figure(copies, sources), "gap": figure(e.open - copies, sources),
            "limited_by": limited_by, "capacity": capacity,
        })

    covered, needed = sum(n for n, _ in packed.values()), sum(e.open for e in eds)
    top = {"covered": ids(*(e["covered"]["fact_ids"] for e in out_editions)),
           "needed": ids(*(e.open_ids for e in eds))}
    top["gap"] = ids(top["needed"], top["covered"])
    return {
        "covered": covered, "needed": needed, "gap": needed - covered, "unit": "copies",
        "at": now.isoformat(), "deadline": deadline.isoformat(), "fact_ids": top,
        "sources": [{"fact_id": i} for i in top["gap"]],
        "assumptions": ["a date-only arrival is at the packing site by the first shift of that day"],
        "priority": {"order": order, "fact_ids": policy_ids},
        "editions": out_editions, "parts": parts,
        "not_shortages": [{"material_id": m, "reason": "phantom BOM line", "fact_ids": found}
                          for m, found in phantom.items()],
        "windows": [{"start": w.start.isoformat(), "end": w.end.isoformat(),
                     "hours": figure(coverage_windows.hours([w]), w.fact_ids)} for w in windows],
        "escalations": alerts,
    }
