"""Usable stock: what kb believes is on hand, minus what waits for a decision.

Three things take booked units out of usable stock:
  - a lot that failed inspection (kb `batch_usable`);
  - a disputed goods receipt. Where kb believes the correction, the correction wins. Where a lower count is on
    file and kb kept the booking (an unsure scan), the difference waits for a decision: it is never usable;
  - a disputed drawing revision that may not be used up, unless the batch is shown to be at the released one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .facts import Facts, believed_ids, day, figure, ids, number

BLOCKED = "blocked pending decision"


@dataclass
class Revision:
    """A revision dispute on one part: which deliveries are shown to be at the released revision."""
    material_id: str
    released: str | None
    fact_ids: list[str]
    by_po: dict = field(default_factory=dict)           # po number -> (revisions, fact ids)
    by_note: dict = field(default_factory=dict)         # delivery note -> (revisions, fact ids)
    by_supplier: dict = field(default_factory=dict)     # supplier id -> (revisions, fact ids)

    def check(self, pos: list[str], supplier: str, note: str = "") -> tuple[bool, list[str]]:
        """(shown to be at the released revision, evidence). No evidence is not proof."""
        found = [self.by_note[note]] if note in self.by_note else [self.by_po[p] for p in pos if p in self.by_po]
        if not found and supplier in self.by_supplier:
            found = [self.by_supplier[supplier]]
        seen = set().union(*(revs for revs, _ in found))
        evidence = ids(self.fact_ids, *(sources for _, sources in found))
        return self.released is not None and seen == {self.released.casefold()}, evidence


def revision_disputes(facts: Facts, parts: set[str]) -> dict[str, Revision]:
    out: dict[str, Revision] = {}
    changes = facts.rows("change_record")
    for b in facts.believed("drawing_rev"):
        material = str(b["key"].get("material_id", ""))
        if material not in parts or not b.get("contradiction"):
            continue
        released = None if b.get("value") is None else str(b["value"]).strip()
        related = [c for c in changes if material in (c.key("material_id"), c.key("child_id"))]
        change_ids = {c.key("change_id") for c in related
                      if (c.key("field") or c.key("change_field")) == "drawing_rev" and released
                      and c.key("new").casefold() == released.casefold()} - {""}
        use_up = [c for c in related if c.key("change_id") in change_ids and c.key("use_up_old")]
        if use_up and all(c.key("use_up_old").lower() in ("y", "yes", "true") for c in use_up):
            continue                                    # the change itself allows the old revision
        out[material] = Revision(material, released,
                                 ids(believed_ids(b), *(c.ids("use_up_old") for c in use_up)))
    if not out:
        return out

    ordered: dict[str, set[str]] = {}
    supplier_of: dict[str, str] = {}
    for line in facts.rows("po_line"):
        ordered.setdefault(line.key("po_number"), set()).add(line.key("material_id"))
        supplier_of[line.key("po_number")] = line.key("supplier_id")
    names = {m: {m} | {x.key("supplier_part_no") for x in facts.rows("part_xref", material_id=m)} - {""}
             for m in out}
    for s in facts.rows("supplier_statement"):
        revision = s.key("revision").casefold()
        if not revision:
            continue
        po, note = s.key("po_number"), s.key("delivery_note")
        material = facts.material(s.key("material_id"))
        if not material:
            named = [m for m in out if any(re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", s.key("value"))
                                           for n in names[m])]
            candidates = named or [m for m in out if m in ordered.get(po, set())]
            material = candidates[0] if len(candidates) == 1 else ""
        if material not in out:
            continue
        dispute = out[material]
        for index, key in ((dispute.by_po, po), (dispute.by_note, note),
                           (dispute.by_supplier, supplier_of.get(po) or s.key("supplier_id"))):
            if key:
                revisions, sources = index.setdefault(key, (set(), []))
                revisions.add(revision)
                sources.extend(s.ids("revision"))
    return out


def stock_position(facts: Facts, material: str, sites: set[str], revision: Revision | None,
                   no_stock_ids: list[str]) -> tuple[dict, list[dict]]:
    """Booked, blocked, corrected and usable units of one part. `no_stock_ids` source a zero."""
    batches = {}
    for row in facts.rows("stock_batch", material_id=material):
        if sites and row.key("site_id") and row.key("site_id") not in sites:
            continue
        if number(row.get("qty")) is not None:
            key = (row.key("site_id"), row.key("batch"))
            if key not in batches or row.as_of >= batches[key].as_of:
                batches[key] = row
    receipts = facts.rows("goods_receipt", material_id=material)
    receipt_of = {r.key("gr_number"): r for r in receipts}

    booked, booked_ids, usable = 0, [], 0
    holds, alerts, fully_held = [], [], set()
    for (_, batch), row in batches.items():
        qty, sources = number(row.get("qty")), row.ids("qty")
        booked += qty
        booked_ids += sources
        receipt = receipt_of.get(batch)
        free = qty
        for b in facts.believed("batch_usable", batch=batch):
            value = number(b.get("value"))
            if not b.get("contradiction") and value is not None:
                continue
            keep = min(qty, max(0, value or 0))         # undecided is not usable
            if keep < free:
                holds.append({"reason": "failed_inspection", "status": BLOCKED, "batch": batch,
                              "qty": figure(free - keep, sources, believed_ids(b))})
                free = keep
        if revision and free > 0:
            ok, evidence = revision.check([receipt.key("po_number")] if receipt else [],
                                          receipt.key("supplier_id") if receipt else "",
                                          receipt.key("delivery_note") if receipt else "")
            if not ok:
                holds.append({"reason": "disputed_revision", "status": BLOCKED, "batch": batch,
                              "qty": figure(free, sources, evidence, receipt.ids("po_number") if receipt else [])})
                free = 0
        if qty > 0 and free == 0 and receipt:
            fully_held.add(receipt.key("delivery_note"))
        usable += free

    # ERP stock is receipts minus consumption, so a wrong booking stays in the total after its batch is packed.
    snapshot = max((d for r in batches.values() if (d := day(r.get("snapshot_date")))), default=None)
    for b in facts.believed("received_qty", material_id=material):
        note = str(b["key"].get("delivery_note", ""))
        if not b.get("contradiction") or note in fully_held:
            continue
        bookings = [r for r in receipts if r.key("delivery_note") == note
                    and number(r.get("qty_received")) is not None
                    and not (snapshot and (d := day(r.get("gr_date"))) and d > snapshot)]
        if not bookings:
            continue
        received = sum(number(r.get("qty_received")) for r in bookings)
        sources = ids(believed_ids(b), *(r.ids("qty_received") for r in bookings))
        counts = [n for i in believed_ids(b) if (n := number(facts.value_of(i))) is not None]
        believed = number(b.get("value"))
        if believed is not None and believed != received:
            change = max(believed - received, -usable)
            usable += change
            holds.append({"reason": "receipt_corrected", "status": "corrected",
                          "qty": figure(change, sources), "delivery_note": note})
        else:
            disputed = min(usable, received - min(counts + [received]))
            if disputed > 0:
                usable -= disputed
                holds.append({"reason": "disputed_receipt", "status": BLOCKED,
                              "qty": figure(disputed, sources), "delivery_note": note})

    for hold in holds:
        alerts.append({"kind": hold["reason"], "material_id": material, "qty": hold["qty"],
                       "fact_ids": hold["qty"]["fact_ids"]})
    basis = ids(booked_ids) or no_stock_ids
    blocked = [h["qty"] for h in holds if h["status"] == BLOCKED]
    out = {"booked": figure(booked, basis),
           "usable": figure(max(0, usable), basis, *(h["qty"]["fact_ids"] for h in holds)),
           "blocked": figure(sum(q["value"] for q in blocked), basis, *(q["fact_ids"] for q in blocked)),
           "holds": holds}
    safety = next((r for r in batches.values() if number(r.get("safety_stock")) is not None), None)
    if safety:
        out["safety_stock"] = {**figure(number(safety.get("safety_stock")), safety.ids("safety_stock")),
                               "basis": "weekly plan units, not launch need", "launch_buffer": False}
    return out, alerts
