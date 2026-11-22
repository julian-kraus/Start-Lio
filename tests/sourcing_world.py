"""Synthetic facts for the coverage tests: two editions, one shared part, one day of packing."""

from datetime import datetime

from plan import contracts
from sourcing import coverage
from sourcing.facts import Facts

NOW = datetime.fromisoformat("2030-01-07T05:00:00+00:00")        # a Monday
DEADLINE = datetime.fromisoformat("2030-01-07T18:00:00+00:00")
NEXT_DAY = datetime.fromisoformat("2030-01-08T10:00:00+00:00")


class World:
    def __init__(self):
        self.current, self.believed, self.issues = [], [], []
        for product, name, demand, rate in (("P-A", "Deluxe", 1100, 100), ("P-B", "Basic", 2100, 200)):
            self.add("plan_edition", {"product_id": product}, edition_name=name, demand=demand, rate_per_hour=rate)
            self.add("packout", {"date": "2030-01-06", "product_id": product}, units_packed=100, site_id="HUB",
                     po_line=f"line {name}")
        for product, part, qty in (("P-A", "CARD", 1), ("P-A", "BOOK", 2), ("P-B", "CARD", 1), ("P-B", "CASE", 1)):
            self.bom(product, part, qty)
        for part, qty in (("CARD", 2500), ("BOOK", 2000), ("CASE", 2000)):
            self.stock(part, qty)
        self.policy = self.add("x_workbook_row", {"record_id": "memo"}, note="Priority by memo: Deluxe first.")
        for weekday in ("Monday", "Tuesday"):
            self.add("shift", {"day": weekday}, shift="06:00-18:00", hours=12)
        self.add("route", {"route_id": "R1"}, origin="PORT", destination="HUB", mode="road", transit_days=1)

    def add(self, fact_type, subject, path="ops/file.csv", as_of=None, **attrs):
        out = {}
        for name, value in attrs.items():
            out[name] = f"f{len(self.current)}"
            self.current.append({"fact_id": out[name], "path": path, "fact_type": fact_type, "subject": subject,
                                 "attribute": name, "value": value, "as_of": as_of, "confidence": 0.9})
        return out

    def drop(self, fact_type, **match):
        self.current = [f for f in self.current if not (
            f["fact_type"] == fact_type and all(f["subject"].get(k) == v for k, v in match.items()))]

    def bom(self, product, part, qty):
        return self.add("bom_line", {"parent_id": product, "child_id": part}, qty_per=qty)

    def stock(self, part, qty, batch=None, **more):
        return self.add("stock_batch", {"batch": batch or f"B-{part}", "material_id": part}, qty=qty,
                        site_id="HUB", **more)

    def receipt(self, batch, part, qty, po="PO-1", supplier="S1", note="N1", **more):
        return self.add("goods_receipt", {"gr_number": batch}, material_id=part, qty_received=qty, po_number=po,
                        supplier_id=supplier, delivery_note=note, **more)

    def ship(self, qty=500, eta="2030-01-06", destination="PORT", ref="T1", part="CARD", status="in_transit",
             po="PO-1", **more):
        ids = self.add("shipment", {"shipment_id": ref}, reference=ref, eta=eta, destination=destination,
                       status=status, material_ids=part, po_numbers=po, **more)
        if qty is not None:
            ids.update(self.add("shipment_qty", {"reference": ref, "material_id": part}, qty_planned=qty))
        return ids

    def believe(self, rule, key, value, winning=(), losing=()):
        self.believed.append({"rule_id": rule, "key": key, "value": value, "winning_fact_ids": list(winning),
                              "losing_fact_ids": list(losing), "contradiction": bool(losing)})

    def run(self, now=NOW, deadline=DEADLINE, **kw):
        result = coverage.compute(Facts(self.current, self.believed, self.issues), now=now, deadline=deadline, **kw)
        contracts.validate("coverage", result)
        check_sources(result, {f["fact_id"] for f in self.current})
        return result


def check_sources(result, known):
    """Every number is a figure whose facts exist, and every fact id named anywhere exists."""
    for name in ("covered", "needed", "gap"):
        assert result["fact_ids"][name], name

    def walk(node, where):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "fact_ids" and isinstance(value, list):
                    assert set(value) <= known, where
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    top = not where and key in ("covered", "needed", "gap")
                    assert top or (key == "value" and node.get("fact_ids")), f"{where}/{key} has no fact_ids"
                else:
                    walk(value, f"{where}/{key}")
        elif isinstance(node, list):
            for item in node:
                assert isinstance(item, bool) or not isinstance(item, (int, float)), f"bare number in {where}"
                walk(item, where)

    walk(result, "")
    assert {s["fact_id"] for s in result["sources"]} <= known


def part(result, name):
    return next(p for p in result["parts"] if p["material_id"] == name)


def edition(result, product):
    return next(e for e in result["editions"] if e["product_id"] == product)


def allocation(result, name):
    return {a["product_id"]: a["allocated"]["value"] for a in part(result, name)["allocation"]}


def alerts(result):
    return {a["kind"] for a in result["escalations"]}


def revised(world, use_up="N"):
    change = world.add("change_record", {"change_id": "C1", "child_id": "CASE", "change_field": "drawing_rev"},
                       new="C", use_up_old=use_up)
    master = world.add("component", {"material_id": "CASE"}, drawing_rev="B")
    world.believe("drawing_rev", {"material_id": "CASE"}, "C", ["derived:" + change["new"]], [master["drawing_rev"]])
    world.drop("stock_batch", material_id="CASE")
    for batch, po, supplier, rev in (("NEW", "PO-C", "SC", "C"), ("OLD", "PO-B", "SB", "B")):
        world.stock("CASE", 1200, batch=batch)
        world.receipt(batch, "CASE", 1200, po=po, supplier=supplier, note=f"N-{batch}")
        world.add("po_line", {"po_number": po, "po_line": 1}, material_id="CASE", supplier_id=supplier)
        world.add("supplier_statement", {"po_number": po, "statement_no": 1}, revision=rev, value=f"rev {rev}")
