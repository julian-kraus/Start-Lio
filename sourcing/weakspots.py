"""Weak spots: the parts that can break the goal, worst first.

    spots = rank(facts, coverage.compute(facts, now=now, deadline=deadline), goal)

Pure: each spot validates against the weak_spot contract.

  copies at risk  open copies that usable stock on hand cannot pack. What is still inbound can fail, so it is
                  not stock. On a shared part the shortage is priced on the most valuable edition first
  money at risk   copies at risk times the goal's value of a missing copy of that edition
  substitute      copies beyond an edition's pre-orders lend their units of a shared part, unless the priority
                  policy puts the lender first or is unclear. A copy that lent keeps its own edition's value, so
                  lending saves the difference and no more
  rank            money at risk times the share no substitute saves (`exposure_eur`). It is the worst order in
                  which the shortage can fall, each after the lending that saves money. No order or lending plan
                  depends on the values, so a higher value can never lower the exposure
  critical        used only by the editions the goal values most, with at most one qualified source

The goal is a goal record: `unit_loss_eur` with `edition`, or `unit_loss_by_edition` keyed by product id or edition
name. Without either, a statement narrows the value only when each of its clauses is one of PROTECTED, GIVEN_UP or
TRADE_OFF. Any other wording values every edition and the goal line says that this is assumed.
"""

from __future__ import annotations

import math
import re
from itertools import permutations

from .coverage import OUTSIDE
from .coverage import editions as plan_editions
from .facts import Facts, Row, figure, ids, number

MAIL = "mail/"                                  # an email cannot list or qualify a source
SENT = re.compile(r"suppliers/[^/]+/documents/")    # nor can a supplier's document, exports beside them can
QUALIFIED = {"status": ("active",), "qualification_status": ("qualified", "approved")}
EVERY_EDITION = ("all", "both")
ASSUMED = "assumed: every edition counts, the goal does not clearly single one out"
CLAUSE = re.compile(r"[.;!]|,(?!\d)|\bbut\b")
# A closed grammar on purpose: a longer list of words to distrust would never be complete.
PROTECTED = (r"(all|every|each)( of)?( the)?( \d[\d,]*)? {name}( copy| copies)?"
             r"( (must|should|has to|have to|needs to|need to))?( (be|is|are|ship|ships|arrive|arrives))? (on|in) time")
GIVEN_UP = (r"((all|every|each) )?{name}( copy| copies)? (can|could|may|might) "
            r"(wait|slip|be late|be later|be delayed|be postponed)")
TRADE_OFF = re.compile(r"((supplier )?goodwill|margins?|costs?|budget) (can|could|may|might) (suffer|slip|rise)"
                       r"|(don't|do not) (bankrupt|overspend)( me| us)?")
SEVERITY = ("critical", "at_risk", "covered", "ok")


def _plain(text) -> str:
    return " ".join(str(text or "").replace("’", "'").split()).lower()


def goal_values(goal: dict, editions: list[dict]) -> tuple[dict[str, float], str]:
    """(value of one missing copy per product, the goal line it comes from). Never guesses a price."""
    labels = {e["product_id"]: {_plain(e["product_id"]), _plain(e["name"])} for e in editions}
    by_edition = goal.get("unit_loss_by_edition")
    values, priced, assumed = dict.fromkeys(labels, 0), set(), False
    if by_edition:
        for label, amount in by_edition.items():
            named = [p for p, known in labels.items() if _plain(label) in known]
            if len(named) != 1:
                raise ValueError(f"the goal prices an unknown edition: {label!r}")
            if named[0] in priced:
                raise ValueError(f"the goal prices one edition twice: {label!r}")
            priced.add(named[0])
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
            named = _stated(_plain(goal.get("statement")), labels)
            if not named:
                named, assumed = list(labels), True
        values.update(dict.fromkeys(named, price))
    prices = ", ".join(f"{e['name']} {values[e['product_id']]:,} EUR" for e in editions)
    statement = " ".join(str(goal.get("statement") or "").split())
    return values, (f"{statement} " if statement else "") + f"(value of a missing copy: {prices}" + (
        f"; {ASSUMED})" if assumed else ")")


def _stated(said: str, labels: dict[str, set[str]]) -> list[str]:
    """The editions a fully recognised statement protects. None for any other wording."""
    kept, given_up = set(), set()
    for clause in filter(None, (c.strip() for c in CLAUSE.split(said))):
        if TRADE_OFF.fullmatch(clause):
            continue
        for pattern, found in ((PROTECTED, kept), (GIVEN_UP, given_up)):
            products = {p for p, known in labels.items() for label in known
                        if re.fullmatch(pattern.format(name=re.escape(label)), clause)}
            if len(products) == 1:
                found |= products
                break
        else:
            return []
    return [p for p in labels if p in kept] if not kept & given_up else []


def _price(amount):
    value = number(amount)
    if value is None or not math.isfinite(value) or value < 0:
        raise ValueError("the goal needs a value per missing copy, zero or more")
    return value


def qualified_sources(facts: Facts, material: str) -> tuple[list[str], list[str], list[str], list[str]]:
    """(suppliers that count as a source, listed ones that do not, the facts behind both, those counted without a
    qualification on file). What a supplier sent can take a qualification away, never give one."""
    listed: dict[str, list[str]] = {}
    for row in facts.rows("supplier_part", material_id=material):
        if row.key("supplier_id") and _internal(row):
            listed.setdefault(row.key("supplier_id"), []).extend(row.ids("primary", "share"))
    good, out, unrecorded, sources = [], [], [], ids(*listed.values())
    for supplier in sorted(listed):
        profile = [r for r in facts.rows("supplier", supplier_id=supplier) if not r.path.startswith(MAIL)]
        counts = True
        for name, accepted in QUALIFIED.items():
            for rows in (profile, [r for r in profile if _internal(r)]):
                stated = [r for r in rows if r.key(name)]
                newest = [r for r in stated if r.as_of == max(s.as_of for s in stated)]
                counts = counts and all(r.key(name).lower() in accepted for r in newest)
                sources = ids(sources, *(r.ids(name) for r in newest))
        (good if counts else out).append(supplier)
        if counts and not any(r.key("qualification_status") for r in profile if _internal(r)):
            unrecorded.append(supplier)
    return good, out, sources, unrecorded


def _internal(row: Row) -> bool:
    return not row.path.startswith(MAIL) and not SENT.match(row.path)


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


def _filled(gap, order: tuple, needs: dict, open_copies: dict) -> dict[str, dict]:
    """Units short per edition if nothing inbound lands and the shortage falls in this order."""
    out, left = {}, gap
    for product in order:
        short = min(left, needs[product])
        out[product] = {"short": short, "held": needs[product] - short, "substituted": 0, "displaced": 0,
                        "qty_per": needs[product] / open_copies[product]}
        left -= short
    return out


def _may_lend(policy: dict, lender: str, borrower: str) -> bool:
    if policy["order"] is None:
        return not policy["fact_ids"]                   # no policy lends, one that cannot be read does not
    return policy["order"].index(borrower) < policy["order"].index(lender)


def _lost(entry: dict) -> int:
    return _copies(entry["short"] - entry["substituted"] + entry["displaced"], entry["qty_per"])


def _cost(units: dict[str, dict], values: dict):
    return sum(_lost(entry) * values[product] for product, entry in units.items())


def _lend(units: dict[str, dict], policy: dict, retail: dict) -> set[str]:
    """Editions later in the order lend the units of their retail copies to earlier ones. Never looks at a value."""
    used, spare = set(), {p: retail[p][0] * e["qty_per"] for p, e in units.items()}
    order = list(units)
    for at, borrower in enumerate(order):
        for lender in reversed(order[at + 1:]):
            if not _may_lend(policy, lender, borrower):
                continue
            to, by = units[borrower], units[lender]
            moved = min(to["short"] - to["substituted"], spare[lender], by["held"])
            if moved > 0:
                to["substituted"] += moved
                by["displaced"] += moved
                by["held"] -= moved
                spare[lender] -= moved
                used.add(lender)
    return used


def _plan(part: dict, values: dict, open_copies: dict, policy: dict, retail: dict) -> tuple[dict, set[str], float]:
    """(units per edition in the worst order after useful lending, who lent, money at risk without lending)."""
    needs = {a["product_id"]: a["need"]["value"] for a in part["allocation"] if a["need"]["value"] > 0}
    users = sorted(needs, key=lambda p: (-values[p] * open_copies[p] / needs[p], p))     # on a tie the first wins
    options, gap = [], part["gap_on_stock"]["value"]
    for order in permutations(users):
        units = _filled(gap, order, needs, open_copies)
        before = _cost(units, values)
        lent = {product: dict(entry) for product, entry in units.items()}
        lenders = _lend(lent, policy, retail)
        after = _cost(lent, values)
        options.append((after, before, lent, lenders) if after < before else (before, before, units, set()))
    _, _, units, lenders = max(options, key=lambda option: option[:2])
    if len(units) > 1 and any(values[p] for p in units):
        for product, entry in units.items():            # a loss worth nothing that falls on retail copies is covered
            if not values[product]:
                own = min(entry["short"] - entry["substituted"],
                          retail[product][0] * entry["qty_per"] - entry["displaced"])
                if own > 0:
                    entry["substituted"] += own
                    entry["displaced"] += own
                    lenders = lenders | {product}
    return units, lenders, max(option[1] for option in options)


def _why(status: str, good: list[str], out: list[str], unrecorded: list[str], risk: dict, names: dict,
         part: dict) -> str:
    kind = "listed" if unrecorded else "qualified"
    sources = (f"one {kind} source ({good[0]})" if len(good) == 1 else
               f"{len(good)} {kind} sources" if good else "no qualified source on file")
    if unrecorded:
        sources += ", qualification not on file" + (f" for {', '.join(unrecorded)}" if len(good) > 1 else "")
    if out:
        sources += f", {', '.join(out)} not qualified"
    if not risk["copies"]:
        spare = round(part["usable"]["value"] - part["need"]["value"], 2)
        text = f"stock on hand packs every open copy with {spare:,} units spare"
    elif status == "covered":
        lenders = " and ".join(names[p] for p in risk["lenders"])
        text = f"{lenders} copies beyond pre-orders cover the {risk['units']:,} units short"
    else:
        text = (f"{risk['open_copies']:,} {' and '.join(names[p] for p in risk['editions'])} copies may have no "
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
    short, lenders, money = _plan(part, values, open_copies, coverage["priority"], retail)
    good, out, source_ids, unrecorded = qualified_sources(facts, part["material_id"])

    basis = ids(part["gap_on_stock"]["fact_ids"], part["need"]["fact_ids"])
    policy_decides = len(short) > 1 and any(entry["short"] for entry in short.values())
    lent_ids = ids(*(retail[p][1] for p in lenders), coverage["priority"]["fact_ids"] if policy_decides else [])
    per_edition, copies, uncovered, lost, unmet = [], 0, 0, {}, {}
    for product, entry in short.items():
        at_risk = _copies(entry["short"], entry["qty_per"])
        without = _copies(entry["short"] - entry["substituted"], entry["qty_per"])
        copies, uncovered = copies + at_risk, uncovered + without
        lost[product], unmet[product] = _lost(entry), without
        per_edition.append({
            "product_id": product, "name": editions[product]["name"],
            "copies_at_risk": figure(at_risk, basis),
            "without_substitute": figure(without, basis, lent_ids),
            "copies_lost": figure(lost[product], basis, lent_ids),
            "value_per_copy_eur": {"value": values[product], "goal_line": goal_line},
        })
    exposure = _cost(short, values)

    top = max(values.values())
    only_protected = bool(short) and top > 0 and all(values[p] == top for p in short)
    if only_protected and len(good) <= 1:
        status = "critical"
    elif uncovered or exposure:
        status = "at_risk"
    else:
        status = "covered" if copies else "ok"
    counted = {p: n for p, n in (lost if exposure else unmet).items() if n and (values[p] or not exposure)}
    risk = {"copies": copies, "open_copies": sum(counted.values()), "exposure": exposure,
            "units": part["gap_on_stock"]["value"], "lenders": sorted(lenders), "editions": list(counted)}
    factor = exposure / money if money else (uncovered / copies if copies else 1)

    used = ids(basis, part["usable"]["fact_ids"], source_ids, lent_ids)
    return {
        "part_id": part["material_id"], "lane": part["material_id"],
        "exposure_eur": round(exposure, 2),
        "reason": _why(status, good, out, unrecorded, risk, {p: e["name"] for p, e in editions.items()}, part),
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
            "qualified_sources": good, "not_qualified": out,
            "qualification_not_on_file": unrecorded, "source_fact_ids": source_ids,
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
