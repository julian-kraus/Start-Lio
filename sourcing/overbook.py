"""Buffer sizing (newsvendor): how many backup units to book. Pure functions, no LLM, no database.

    result = size(outcomes=if_primary_fails(p_fail(card_a), short_units), unit_price=price(facts, "B", part),
                  loss_per_copy=goal_loss(goal), units_per_copy=..., p_delivers=keeps_promise(card_b),
                  lot_size=..., moq=..., available=...)

  quantile   1 - C_o / (p x C_u). C_o: cost of a leftover part from the price facts, less salvage.
             C_u: loss per missing copy / units per copy. p: B delivers and passes quality.
  target     the smallest shortfall that this share of outcomes does not exceed
  quantity   the cheapest orderable amount next to the target: nothing, or whole lots of at least the MOQ and at
             most what B has. The expected cost C_o x Q - p x C_u x E[min(Q, shortfall)] is convex, so that is the
             best of all orderable amounts, and a higher loss per copy never books less.

A price in another currency needs a sourced exchange rate. It is never read as EUR.
"""

from __future__ import annotations

import math
import re
from typing import Iterable, Mapping, Sequence

from sourcing.hedge import NUMBER, Input, ModelError, amount, parse_number, positive, row, unique

SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "zł": "PLN"}
CURRENCIES = frozenset(
    "EUR USD GBP PLN CHF CZK HUF RON SEK DKK NOK CNY JPY KRW INR TWD VND HKD SGD THB MYR IDR TRY MXN BRL CAD AUD"
    .split())
CODE = r"(€|\$|£|zł|[a-z]{3})"
PER_ONE = r"(?:\s*(?:/|per\s|je\s|za\s)\s*(?:1\s*)?(?:pcs?|pieces?|piece|units?|stück|stk|szt|each|ea)\.?)?"
PRICE = re.compile(rf"(?:{CODE}\s*)?({NUMBER})(?:\s*{CODE})?{PER_ONE}")
PER_MANY = re.compile(r"(?:/|\bper\b|\bje\b|\bza\b)\s*(\d[\d.,\s]*)")
PER_WORD = re.compile(r"\b(?:hundred|thousand|dozen|hundert|tausend|dutzend|sto|tysiąc|mille)\b")
COUNT = re.compile(rf"(?:min\.?\s*|minimum\s+)?({NUMBER})\s*(?:pcs?|pieces?|units?|stück|stk|szt)?\.?")
MAIL = "mail/"          # an email may make an offer, it cannot change the price list
TOLERANCE = 1e-9


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _newest(rows: list[Mapping], what: str) -> tuple[str, list[Mapping]]:
    if not rows:
        raise ModelError(f"no {what} fact")
    last = max(_text(f.get("as_of")) for f in rows)
    rows = [f for f in rows if _text(f.get("as_of")) == last]
    values = {_text(f.get("value")) for f in rows}
    if len(values) != 1:
        raise ModelError(f"{what} facts disagree: {sorted(values)}")
    return values.pop(), rows


def _cited(rows: Iterable[Mapping]) -> list[dict]:
    ids = sorted({_text(f.get("fact_id")) for f in rows} - {""})
    if not ids:
        raise ModelError("a fact without a fact_id cannot be cited")
    return [{"fact_id": i} for i in ids]


def _terms(facts: list[Mapping], supplier_id: str, part_id: str, offer_ref: str | None) -> list[Mapping]:
    """The facts of one offer, or else of the supplier's price list line for this part."""
    kind = "offer" if offer_ref is not None else "supplier_part"

    def key(fact, name) -> str:
        return _text((fact.get("subject") or {}).get(name))

    rows = [f for f in facts if f.get("fact_type") == kind and key(f, "supplier_id") == supplier_id]
    if offer_ref is None:
        return [f for f in rows
                if key(f, "material_id") == part_id and not _text(f.get("path")).startswith(MAIL)]
    rows = [f for f in rows if key(f, "offer_ref") == offer_ref]
    items = {key(f, "item") for f in rows if part_id in (key(f, "item"), key(f, "material_id"))
             or (f.get("attribute") == "material_id" and _text(f.get("value")) == part_id)}
    return [f for f in rows if key(f, "item") in items]


def price(facts: Iterable[Mapping], supplier_id: str, part_id: str, *, offer_ref: str | None = None,
          fx: Input | None = None) -> Input:
    """EUR per unit from `kb.current_facts` rows, citing the facts. `fx` is EUR per unit of the price's currency."""
    facts = list(facts)
    terms = _terms(facts, supplier_id, part_id, offer_ref)
    text, used = _newest([f for f in terms if f.get("attribute") == "unit_price"], "unit_price")
    found = PRICE.fullmatch(text.lower())
    if not found:
        raise ModelError(f"cannot read a price per unit from {text!r}")
    before, digits, after = found.groups()
    stated = {SYMBOLS.get(code, code.upper()) for code in (before, after) if code}
    if stated - CURRENCIES:
        raise ModelError(f"unknown currency in {text!r}")
    stated |= {u for f in used if (u := _text(f.get("unit")).upper()) in CURRENCIES}

    if basis := [f for f in terms if f.get("attribute") == "price_basis"]:
        said, basis = _newest(basis, "price_basis")
        per = PER_MANY.search(said.lower())
        if PER_WORD.search(said.lower()) or (per and parse_number(per.group(1).strip(" .,"), grouped=True) != 1):
            raise ModelError(f"the price is not per unit: {said!r}")
        used = used + basis
    if not stated:
        own = [f for f in terms if f.get("attribute") == "currency"]
        listed = [f for f in facts if f.get("fact_type") == "supplier" and f.get("attribute") == "currency"
                  and _text((f.get("subject") or {}).get("supplier_id")) == supplier_id
                  and not _text(f.get("path")).startswith(MAIL)]
        code, basis = _newest(own or listed, "currency")
        stated, used = {code.upper()}, used + basis
    if len(stated) != 1:
        raise ModelError(f"the price names several currencies: {sorted(stated)}")

    currency = stated.pop()
    value, refs, note = parse_number(digits), _cited(used), f"quoted as {text} ({currency})"
    if currency != "EUR":
        if fx is None:
            raise ModelError(f"the price is in {currency}: give a sourced exchange rate to EUR")
        value *= positive(fx, "exchange rate")
        refs, note = refs + fx.refs, f"{note}, x {fx.value} EUR per {currency}"
    return Input(value, refs, note, computed=fx is not None and fx.computed)


def order_limit(facts: Iterable[Mapping], supplier_id: str, part_id: str, attribute: str,
                offer_ref: str | None = None) -> Input | None:
    """`lot_size`, `moq` or the offered `qty` as a count. None: the facts state none."""
    if attribute not in ("lot_size", "moq", "qty"):
        raise ModelError(f"not an order limit: {attribute!r}")
    rows = [f for f in _terms(list(facts), supplier_id, part_id, offer_ref)
            if f.get("attribute") == attribute and _text(f.get("value"))]
    if not rows:
        return None
    text, rows = _newest(rows, attribute)
    found = COUNT.fullmatch(text.lower())
    if not found:
        raise ModelError(f"cannot read {attribute} from {text!r}")
    return Input(parse_number(found.group(1), grouped=True), _cited(rows), f"quoted as {text}")


def if_primary_fails(p_fail: Input, short_if_failed: Input,
                     short_if_kept: Input | None = None) -> list[tuple[Input, Input]]:
    """Two outcomes of (units short, probability). Without `short_if_kept` a kept promise leaves nothing short."""
    p = amount(p_fail, "p_fail", 1)
    kept = short_if_kept or Input(0, short_if_failed.source, "nothing short when the primary delivers",
                                  short_if_failed.computed)
    return [(kept, Input(1 - p, p_fail.source, "the primary delivers", p_fail.computed)), (short_if_failed, p_fail)]


def critical_ratio(leftover: float, loss_per_unit: float, p: float) -> float:
    """1 - C_o / (p x C_u), or 0 where a unit costs at least what it can save."""
    saved = p * loss_per_unit
    return 0.0 if leftover >= saved else 1 - leftover / saved


def quantile(outcomes: Sequence[tuple[float, float]], share: float) -> float:
    if share <= 0:
        return 0.0
    reached, units = 0.0, 0.0
    for units, p in sorted(o for o in outcomes if o[1] > 0):
        reached += p
        if reached >= share - TOLERANCE:
            break
    return units


def size(*, outcomes: Sequence[tuple[Input, Input]], unit_price: Input, loss_per_copy: Input,
         units_per_copy: Input, p_delivers: Input, lot_size: Input | None = None, moq: Input | None = None,
         available: Input | None = None, salvage_per_unit: Input | None = None) -> dict:
    """Units to book, with the drill-down."""
    spread = [(amount(units, "units short"), amount(p, "probability", 1)) for units, p in outcomes]
    if not math.isclose(sum(p for _, p in spread), 1, abs_tol=1e-6):
        raise ModelError("the probabilities of the shortfall must add up to 1")
    cost, loss = amount(unit_price, "unit price"), amount(loss_per_copy, "loss per copy")
    p, per_copy = amount(p_delivers, "p_delivers", 1), positive(units_per_copy, "units per copy")

    inputs = [row("unit_price", "Cost per unit", unit_price, "EUR"),
              row("loss_per_copy", "Loss per missing copy", loss_per_copy, "EUR"),
              row("units_per_copy", "Units per copy", units_per_copy, "units"),
              row("p_delivers", "Chance the backup delivers and passes quality", p_delivers)]
    for n, (units, chance) in enumerate(outcomes, 1):
        inputs += [row(f"shortfall_{n}", f"Units short, outcome {n}", units, "units"),
                   row(f"probability_{n}", f"Chance of outcome {n}", chance)]

    salvaged, lot, least, most = 0.0, 1, 0, math.inf
    if salvage_per_unit is not None:
        salvaged = amount(salvage_per_unit, "salvage per unit", cost)
        inputs.append(row("salvage_per_unit", "Salvage of a leftover unit", salvage_per_unit, "EUR"))
    if lot_size is not None:
        lot = positive(lot_size, "lot size")
        if lot != int(lot):
            raise ModelError("lot size must be a whole number")
        lot = int(lot)
        inputs.append(row("lot_size", "Lot size", lot_size, "units"))
    if moq is not None:
        least = math.ceil(amount(moq, "MOQ"))
        inputs.append(row("moq", "Minimum order", moq, "units"))
    if available is not None:
        most = math.floor(amount(available, "available"))
        inputs.append(row("available", "Units on offer", available, "units"))

    leftover, per_unit = cost - salvaged, loss / per_copy
    share = critical_ratio(leftover, per_unit, p)
    target = math.ceil(round(quantile(spread, share), 6))

    low = max(math.ceil(least / lot), 1) * lot
    high = most if most == math.inf else most // lot * lot
    options = {0}
    if low <= high:
        options |= {min(max(x, low), high) for x in (target // lot * lot, math.ceil(target / lot) * lot)}

    def expected_cost(q: float) -> float:
        return leftover * q - p * per_unit * sum(chance * min(q, units) for units, chance in spread)

    best = 0
    for q in sorted(options):
        if expected_cost(q) < expected_cost(best) - TOLERANCE:      # a tie goes to the smaller order
            best = q

    if not target:
        why = "Book nothing: at this loss and reliability no backup unit is worth its cost."
    elif low > high:
        why = "Book nothing: the units on offer do not make one order that meets lot size and MOQ."
    else:
        why = f"Book {best:,} units for a shortfall of {target:,} units at the {share:.2%} quantile"
        if best > target:
            why += f", {best - target:,} more for lot size or MOQ"
        elif best < target:
            why += (f", leaving {target - best:,} uncovered: that is all B can deliver" if best == high else
                    f", leaving {target - best:,} uncovered: the next orderable amount costs more than it saves")
        why += "."

    used = [name for name, given in (("lot_size", lot_size), ("moq", moq), ("available", available))
            if given is not None]
    odds = [f"{name}_{n}" for n in range(1, len(outcomes) + 1) for name in ("shortfall", "probability")]
    steps = [
        {"name": "leftover_cost", "text": "Cost of a leftover unit", "value": leftover, "unit": "EUR",
         "source": "unit price minus salvage" if salvage_per_unit else "unit price",
         "from": ["unit_price", *(["salvage_per_unit"] if salvage_per_unit else [])]},
        {"name": "loss_per_unit", "text": "Loss per missing unit", "value": per_unit, "unit": "EUR",
         "source": "loss per copy / units per copy", "from": ["loss_per_copy", "units_per_copy"]},
        {"name": "service_level", "text": "Shortfall quantile to cover", "value": share, "unit": "",
         "source": "1 - C_o / (p x C_u)", "from": ["leftover_cost", "loss_per_unit", "p_delivers"]},
        {"name": "target", "text": "Shortfall at that quantile", "value": target, "unit": "units",
         "source": "smallest shortfall that reaches the quantile", "from": ["service_level", *odds]},
        {"name": "quantity", "text": "Units to book", "value": best, "unit": "units",
         "source": "cheapest orderable amount next to the target",
         "from": ["target", "leftover_cost", "loss_per_unit", "p_delivers", *odds, *used]},
    ]
    return {"quantity": best, "target_units": target, "extra_units": max(0, best - target),
            "uncovered_units": max(0, target - best), "service_level": share, "p_delivers": p,
            "leftover_cost_eur": leftover, "loss_per_unit_eur": per_unit, "order_value_eur": round(best * cost, 2),
            "rationale": why, "drilldown": {"inputs": inputs, "computed": steps},
            "sources": unique([ref for r in inputs for ref in r["refs"]])}


def recommendation(result: Mapping, part_id: str, lead_time_h: Input, **fields) -> dict:
    """The `recommendation` contract, kind buffer. The contract wants the lead time of the timing chain with it."""
    buffer = {"demand": result["target_units"], "lead_time_h": amount(lead_time_h, "lead time"),
              "service_level": result["service_level"], "safety_units": result["extra_units"],
              "sources": result["sources"]}
    drilldown = {**result["drilldown"],
                 "inputs": result["drilldown"]["inputs"] + [row("lead_time_h", "Lead time", lead_time_h, "h")]}
    return {**fields, "kind": "buffer", "part_id": part_id, "quantity": result["quantity"], "buffer": buffer,
            "drilldown": drilldown, "rationale": result["rationale"],
            "sources": unique(result["sources"] + lead_time_h.refs)}
