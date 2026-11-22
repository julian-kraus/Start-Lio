"""Weak spots: the parts that can break the goal, worst first.

    spots = rank(facts, coverage.compute(facts, now=now, deadline=deadline), goal)

Pure: each spot validates against the weak_spot contract.

  copies at risk  open copies that usable stock on hand cannot pack. What is still inbound can fail, so it is
                  not stock. On a shared part the shortage is priced on the most valuable edition first
  money at risk   copies at risk times the goal's value of a missing copy of that edition
  substitute      copies beyond an edition's pre-orders lend their units of a shared part to editions the goal
                  values higher, unless the priority policy puts the lender first
  rank            money at risk times the share no substitute covers (`exposure_eur`)
  critical        used only by the editions the goal values most, with at most one qualified source

The goal is a goal record: `unit_loss_eur` with `edition` (or the editions its statement names, or all of them),
or `unit_loss_by_edition` keyed by product id or edition name.
"""

from __future__ import annotations

import math
import re

from .coverage import OUTSIDE
from .coverage import editions as plan_editions
from .facts import Facts, Row, figure, ids, number

MAIL = "mail/"                                  # an email cannot list or qualify a source
QUALIFIED = {"status": ("active",), "qualification_status": ("qualified", "approved")}
EVERY_EDITION = ("all", "both")
SEVERITY = ("critical", "at_risk", "covered", "ok")


def _plain(text) -> str:
    return " ".join(str(text or "").replace("’", "'").split()).lower()


def goal_values(goal: dict, editions: list[dict]) -> tuple[dict[str, float], str]:
    """(value of one missing copy per product, the goal line it comes from). Never guesses a price."""
    labels = {e["product_id"]: {_plain(e["product_id"]), _plain(e["name"])} for e in editions}
    by_edition = goal.get("unit_loss_by_edition")
    values = dict.fromkeys(labels, 0)
    if by_edition:
        for label, amount in by_edition.items():
            named = [p for p, known in labels.items() if _plain(label) in known]
            if len(named) != 1:
                raise ValueError(f"the goal prices an unknown edition: {label!r}")
            values[named[0]] = _price(amount)
    else:
        price, edition = _price(goal.get("unit_loss_eur")), _plain(goal.get("edition"))
        if edition in EVERY_EDITION:
            named = list(labels)
        elif edition:
            named = [p for p, known in labels.items() if edition in known]
            if not named:
                raise ValueError(f"the goal names an unknown edition: {goal['edition']!r}")
        else:
            said = _plain(goal.get("statement"))
            named = [p for p, known in labels.items()
                     if any(re.search(rf"(?<!\w){re.escape(k)}(?!\w)", said) for k in known)] or list(labels)
        values.update(dict.fromkeys(named, price))
    prices = ", ".join(f"{e['name']} {values[e['product_id']]:,} EUR" for e in editions)
    statement = " ".join(str(goal.get("statement") or "").split())
    return values, (f"{statement} " if statement else "") + f"(value of a missing copy: {prices})"


def _price(amount):
    value = number(amount)
    if value is None or not math.isfinite(value) or value < 0:
        raise ValueError("the goal needs a value per missing copy, zero or more")
    return value


def qualified_sources(facts: Facts, material: str) -> tuple[list[str], list[str], list[str]]:
    """(suppliers that count as a source, listed ones that do not, the facts behind both)."""
    listed: dict[str, list[str]] = {}
    for row in facts.rows("supplier_part", material_id=material):
        if row.key("supplier_id") and not row.path.startswith(MAIL):
            listed.setdefault(row.key("supplier_id"), []).extend(row.ids("primary", "share"))
    good, out, sources = [], [], ids(*listed.values())
    for supplier in sorted(listed):
        profile = [r for r in facts.rows("supplier", supplier_id=supplier) if not r.path.startswith(MAIL)]
        counts = True
        for name, accepted in QUALIFIED.items():
            stated = [r for r in profile if r.key(name)]
            newest = [r for r in stated if r.as_of == max(s.as_of for s in stated)]
            counts = counts and all(r.key(name).lower() in accepted for r in newest)
            sources = ids(sources, *(r.ids(name) for r in newest))
        (good if counts else out).append(supplier)
    return good, out, sources


def retail_copies(facts: Facts, edition: dict) -> tuple[int, list[str]]:
    """Open copies nobody pre-ordered. Packed copies serve pre-orders first; no pre-order fact lends nothing."""
    rows: list[Row] = [r for r in facts.rows("plan_edition", product_id=edition["product_id"])
                       if not r.path.startswith(OUTSIDE) and number(r.get("preorders")) is not None]
    if not rows:
        return 0, []
    beyond = edition["demand"]["value"] - max(edition["packed"]["value"],
                                              *(number(r.get("preorders")) for r in rows))
    sources = ids(edition["demand"]["fact_ids"], edition["packed"]["fact_ids"],
                  *(r.ids("preorders") for r in rows))
    return min(edition["open"]["value"], max(0, beyond)), sources


def _copies(units, qty_per) -> int:
    return math.ceil(round(units / qty_per, 6))         # a fractional quantity must not add a copy


def _shortage(part: dict, values: dict, open_copies: dict) -> dict[str, dict]:
    """Units short per edition if nothing inbound lands, priced on the most valuable edition first."""
    users = [a for a in part["allocation"] if a["need"]["value"] > 0]
    out, left = {}, part["gap_on_stock"]["value"]
    for user in sorted(users, key=lambda a: (-values[a["product_id"]], a["product_id"])):
        product, need = user["product_id"], user["need"]["value"]
        short = min(left, need)
        out[product] = {"need": need, "short": short, "held": need - short, "substituted": 0,
                        "qty_per": need / open_copies[product]}
        left -= short
    return out


def _substitute(short: dict[str, dict], values: dict, order: list[str] | None, retail: dict) -> set[str]:
    """Let retail copies absorb a shared part's shortage. Returns the editions whose retail copies were used."""
    if len(short) < 2:
        return set()
    top = max(values[p] for p in short)
    spare = {p: retail[p][0] * e["qty_per"] for p, e in short.items() if values[p] < top}
    used = set()

    def absorb(borrower: str, lender: str, limit: float):
        want = short[borrower]["short"] - short[borrower]["substituted"]
        units = min(want, spare[lender], limit)
        if units > 0:
            short[borrower]["substituted"] += units
            spare[lender] -= units
            used.add(lender)
        return units

    for borrower in short:                              # most valuable first
        for lender in reversed(short):                  # least valuable lends first
            if lender not in spare or values[lender] >= values[borrower]:
                continue
            if order and order.index(lender) < order.index(borrower):
                continue                                # the policy serves the lender first
            short[lender]["held"] -= absorb(borrower, lender, short[lender]["held"])
    for product in spare:
        absorb(product, product, short[product]["short"])       # its own loss falls on retail copies
    return used


def _why(status: str, good: list[str], out: list[str], risk: dict, names: dict, part: dict) -> str:
    sources = (f"one qualified source ({good[0]})" if len(good) == 1 else
               f"{len(good)} qualified sources" if good else "no qualified source on file")
    if out:
        sources += f", {', '.join(out)} not qualified"
    if not risk["copies"]:
        spare = round(part["usable"]["value"] - part["need"]["value"], 2)
        text = f"stock on hand packs every open copy with {spare:,} units spare"
    elif status == "covered":
        lenders = " and ".join(names[p] for p in risk["lenders"])
        text = f"{lenders} copies beyond pre-orders cover the {risk['units']:,} units short"
    else:
        text = (f"{risk['open_copies']:,} {' and '.join(names[p] for p in risk['editions'])} copies have no "
                f"stock on hand, " + (f"{round(risk['exposure']):,} EUR at risk" if risk["exposure"] else
                                        "and the goal puts no value on them"))
        if risk["lenders"]:
            text += " after lending"
    lead = "No substitute, " if status == "critical" else ""
    return " ".join(f"{lead}{sources}: {text}.".split())


def _editions(facts: Facts) -> dict[str, dict]:
    """Every edition in the plan: coverage may report fewer, but a shared part is split over all of them."""
    return {e.product_id: {"product_id": e.product_id, "name": e.name, "demand": figure(e.demand, e.demand_ids),
                           "packed": figure(e.packed, e.open_ids), "open": figure(e.open, e.open_ids)}
            for e in plan_editions(facts)[0]}


def _spot(facts: Facts, part: dict, coverage: dict, editions: dict, values: dict, goal_line: str,
          retail: dict) -> dict:
    open_copies = {p: e["open"]["value"] for p, e in editions.items()}
    short = _shortage(part, values, open_copies)
    lenders = _substitute(short, values, coverage["priority"]["order"], retail)
    good, out, source_ids = qualified_sources(facts, part["material_id"])

    basis = ids(part["gap_on_stock"]["fact_ids"], part["need"]["fact_ids"])
    lent_ids = ids(*(retail[p][1] for p in lenders), coverage["priority"]["fact_ids"] if lenders else [])
    per_edition, copies, uncovered, money, exposure = [], 0, 0, 0, 0
    for product, entry in short.items():
        at_risk = _copies(entry["short"], entry["qty_per"])
        without = _copies(entry["short"] - entry["substituted"], entry["qty_per"])
        copies, uncovered = copies + at_risk, uncovered + without
        money, exposure = money + at_risk * values[product], exposure + without * values[product]
        per_edition.append({
            "product_id": product, "name": editions[product]["name"],
            "copies_at_risk": figure(at_risk, basis),
            "without_substitute": figure(without, basis, lent_ids),
            "value_per_copy_eur": {"value": values[product], "goal_line": goal_line},
        })

    top = max(values.values())
    only_protected = bool(short) and top > 0 and all(values[p] == top for p in short)
    if only_protected and len(good) <= 1:
        status = "critical"
    elif uncovered:
        status = "at_risk"
    else:
        status = "covered" if copies else "ok"
    risk = {"copies": copies, "open_copies": uncovered, "exposure": exposure,
            "units": part["gap_on_stock"]["value"], "lenders": sorted(lenders),
            "editions": [e["product_id"] for e in per_edition if e["without_substitute"]["value"]]}
    factor = exposure / money if money else (uncovered / copies if copies else 1)

    used = ids(basis, part["usable"]["fact_ids"], source_ids, lent_ids)
    return {
        "part_id": part["material_id"], "lane": part["material_id"],
        "exposure_eur": round(exposure, 2),
        "reason": _why(status, good, out, risk, {p: e["name"] for p, e in editions.items()}, part),
        "detail": {
            "status": status,
            "copies_at_risk": figure(copies, basis),
            "without_substitute": figure(uncovered, basis, lent_ids),
            "units_short_on_stock": part["gap_on_stock"],
            "units_short_with_inbound": part["gap"],
            "money_at_risk_eur": {**figure(money, basis), "goal_line": goal_line},
            "no_substitute": {"value": round(factor, 4), "fact_ids": ids(basis, lent_ids)},
            "editions": per_edition,
            "substitute": [{"product_id": p, "lendable_copies": figure(retail[p][0], retail[p][1])}
                           for p in sorted(lenders)],
            "qualified_sources": good, "not_qualified": out, "source_fact_ids": source_ids,
        },
        "sources": [{"goal_line": goal_line}] + [{"fact_id": i} for i in used],
    }


def rank(facts: Facts, coverage: dict, goal: dict) -> list[dict]:
    """Every part in the coverage, by exposure; on a tie the more severe status, then more copies at risk."""
    editions = _editions(facts)
    values, goal_line = goal_values(goal, list(editions.values()))
    retail = {product: retail_copies(facts, edition) for product, edition in editions.items()}
    spots = [_spot(facts, p, coverage, editions, values, goal_line, retail) for p in coverage["parts"]]
    spots.sort(key=lambda s: (-s["exposure_eur"], SEVERITY.index(s["detail"]["status"]),
                              -s["detail"]["copies_at_risk"]["value"], s["part_id"]))
    return [{**spot, "rank": n} for n, spot in enumerate(spots, 1)]
