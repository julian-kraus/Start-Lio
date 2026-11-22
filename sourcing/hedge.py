"""Hedge rule: is a backup worth its commitment before the primary fails? Pure functions, no LLM, no database.

    result = decide(p_fail=p_fail(card_a), copies_at_risk=Input(n, {"fact_id": ...}), loss_per_copy=goal_loss(goal),
                    units=Input(...), unit_price=overbook.price(...), units_per_copy=Input(...),
                    cost_model=Input("deposit_only", ...), deposit=Input("25%", ...), goal=goal)

  loss avoided   P(A fails) x copies the backup covers (at most those at risk) x loss per copy
  commitment     deposit_only: the deposit, because B can be cancelled. full_order: order value minus salvage
  hedge          loss avoided > commitment
  break-even     commitment / (copies x loss)
  protect margin a goal that says so also counts what the order costs when it is used: the cost becomes
                 (1 - P) x commitment + P x order value. That is never less, so this goal never hedges more.

Every input carries its source: a fact id, a goal line or a labelled scenario input. None has a guessed default.
The remainder after the deposit is its own action, shaped for approval routing (amount_eur, amount_source).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from sourcing import goal_margin

ORIGINS = ("fact_id", "goal_line", "scenario_input")
COST_MODELS = ("deposit_only", "full_order")
NUMBER = r"\d+(?:[.,\s]\d+)*"
EURO = r"(?:€|eur|euros?)"
DEPOSIT = r"(?:deposit|prepayment|down payment|advance payment|anzahlung)"
NOW = r"(?:upfront|up front|in advance|now|(?:on|with|upon) (?:order|acceptance|your acceptance))"
# the whole text must state the deposit: a share that is a balance, a limit or negated is not one
SHARE = re.compile(rf"(?:(?:an? |the )?{DEPOSIT}(?: is| of|:)? )?({NUMBER}) ?(?:%|percent|per cent|prozent)"
                   rf"(?: {DEPOSIT})?(?: of (?:the )?(?:order(?: value)?|total))?"
                   rf"(?:,? (?:due |payable )?{NOW})?\.?")
FACTS_SHOWN = 3     # the rest are in `refs`


class ModelError(ValueError):
    pass


@dataclass(frozen=True)
class Input:
    value: float | str
    source: dict | list
    note: str = ""
    computed: bool = False      # output of another sourcing model, which may add labels to what it cites

    def __post_init__(self):
        refs = self.refs
        if not refs or not all(isinstance(r, Mapping) for r in refs):
            raise ModelError("an input needs a source")
        if not any(r.get(key) not in (None, "") for r in refs for key in ORIGINS):
            raise ModelError("a source is a fact id, a goal line or a labelled scenario input")

    @property
    def refs(self) -> list[dict]:
        return list(self.source) if isinstance(self.source, (list, tuple)) else [self.source]


def unique(refs: Iterable[dict]) -> list[dict]:
    return list({json.dumps(r, sort_keys=True, default=str): r for r in refs}.values())


def amount(given: Input, name: str, most: float = math.inf) -> float:
    if not isinstance(given, Input):
        raise ModelError(f"{name} must be an Input with its source")
    value = given.value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not 0 <= value <= most:
        raise ModelError(f"{name} must be a number from 0 to {most}, got {value!r}")
    return value


def positive(given: Input, name: str) -> float:
    if not (value := amount(given, name)):
        raise ModelError(f"{name} must be above 0")
    return value


def parse_number(text, grouped: bool = False) -> float:
    """A number quoted verbatim. `grouped`: "10,000" is a count or a total, so the separator groups thousands."""
    if isinstance(text, (int, float)) and not isinstance(text, bool) and math.isfinite(text):
        return float(text)
    raw = str(text).strip()
    if re.fullmatch(r"\d{1,3}(?:\s\d{3})+(?:[.,]\d+)?", raw):
        head = re.match(r"[\d\s]+", raw).group()
        return float(re.sub(r"\s", "", head) + raw[len(head):].replace(",", "."))
    if re.fullmatch(r"\d+", raw):
        return float(raw)
    for thousands, decimal in ((",", "."), (".", ",")):
        group = rf"[1-9]\d{{0,2}}(?:{re.escape(thousands)}\d{{3}})"
        if re.fullmatch(rf"{group}+{re.escape(decimal)}\d+|{group}{{2,}}", raw):
            return float(raw.replace(thousands, "").replace(decimal, "."))
        if re.fullmatch(group, raw):
            if grouped:
                return float(raw.replace(thousands, ""))
            raise ModelError(f"{raw!r} can be thousands or decimals")
    if re.fullmatch(r"\d+[.,]\d+", raw):
        return float(raw.replace(",", "."))
    raise ModelError(f"cannot read a number from {text!r}")


def deposit_amount(given: Input, order_value: Input) -> Input:
    """The deposit in EUR from a number, a share of the order ("25%") or a quoted EUR amount."""
    order = amount(order_value, "order value")
    if isinstance(given.value, str):
        text = " ".join(given.value.lower().split())
        if stated := SHARE.fullmatch(text):
            share = parse_number(stated.group(1))
            if share > 100:
                raise ModelError(f"deposit is more than the order: {given.value!r}")
            value = order * share / 100
        else:
            found = re.fullmatch(rf"{EURO}\s*({NUMBER})|({NUMBER})\s*{EURO}", text)
            if not found:
                raise ModelError(f"cannot read a deposit in EUR from {given.value!r}")
            value = parse_number(found.group(1) or found.group(2), grouped=True)
    else:
        value = amount(given, "deposit")
    if round(value, 2) > round(order, 2):
        raise ModelError("deposit is more than the order")
    return Input(round(value, 2), given.source, f"quoted as {given.value}")


def _behind_average(start: Mapping, history) -> list[dict]:
    """The facts behind the all-supplier starting score, if `history` is the one the card was computed from."""
    judged = {sid: s.promises for sid, s in history.suppliers.items() if s.promises}
    counts = {sid: {"kept": sum(p["kept"] for p in found), "n": len(found)} for sid, found in judged.items()}
    if counts != start.get("by_supplier"):
        raise ModelError("the history is not the one this reliability card was computed from")
    return [{"fact_id": i} for i in sorted({i for found in judged.values() for p in found for i in p["fact_ids"]})]


def keeps_promise(card: Mapping, history=None) -> Input:
    """P(a supplier delivers and passes quality): the reliability score, where a failed inspection breaks a promise.
    Without deliveries of its own it is the all-supplier average: pass the `history` so that its facts are cited."""
    score = card.get("score") or {}
    if "value" not in score:
        raise ModelError(f"reliability of {card.get('supplier_id')} is unknown")
    refs = list(score.get("sources") or [])
    start = score.get("starting_point") or {}
    if start.get("kind") == "all_suppliers" and not (score.get("history") or {}).get("n"):
        if history is None:
            raise ModelError(f"{card.get('supplier_id')} has no deliveries: give the history behind the average")
        refs = unique(_behind_average(start, history) + refs)
    note = f"reliability of {card.get('supplier_id')}: on time, in full, no failed inspection"
    quality = [m for m in score.get("measures", []) if m.get("measure") == "quality" and m.get("n")]
    if quality:
        note += f" (quality {quality[0]['kept']} of {quality[0]['n']})"
    return Input(score["value"], refs, note, computed=True)


def p_fail(card: Mapping, history=None) -> Input:
    kept = keeps_promise(card, history)
    return Input(1 - amount(kept, "reliability", 1), kept.source, f"1 minus {kept.note}", computed=True)


def goal_loss(goal: Mapping, edition: str | None = None) -> Input:
    """Loss per missing copy, only ever from the goal, with the origin its goal line records."""
    prices, value = goal.get("unit_loss_by_edition"), goal.get("unit_loss_eur")
    wanted = str(edition or "").casefold()
    if prices:
        found = [v for k, v in prices.items() if str(k).casefold() == wanted]
        if len(found) != 1:
            raise ModelError(f"the goal gives no single loss for edition {edition!r}")
        value = found[0]
    else:
        named = str(goal.get("edition") or "").casefold()
        if wanted and named not in ("", "all", "both", wanted):
            raise ModelError(f"the goal gives no loss for edition {edition!r}")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ModelError("the goal gives no loss per missing copy")
    if lines := [x for x in goal.get("goal_lines") or [] if x.get("field") == "value"]:
        return Input(value, _stated(goal, lines, value, wanted))
    line = f"{goal.get('statement') or ''} (value of a missing copy: {value:,} EUR)".strip()
    return Input(value, {"goal_line": line, "value": value})


def _stated(goal: Mapping, lines: list[Mapping], value: float, wanted: str) -> dict:
    """The confirmed line behind a value. A pre-filled one stays a scenario input: the person did not say it."""
    found = [x for x in lines if x.get("value") == value
             and (not wanted or str(x.get("edition") or "all").casefold() in (wanted, "all", "both"))]
    if len(found) != 1 or not found[0].get("goal_line"):
        raise ModelError(f"the goal has no single line that states a loss of {value:,} EUR")
    source = {"goal_line": found[0]["goal_line"], "value": value}
    source.update({key: goal[key] for key in ("goal_id", "author", "at") if goal.get(key)})
    if label := found[0].get("scenario_input"):
        source["scenario_input"] = label
    return source


def margin_line(goal: Mapping | None) -> str | None:
    """The goal line that protects margin. Words on margin that cannot be read are refused, not guessed."""
    line, unclear = goal_margin.read(goal)
    if unclear:
        raise ModelError(f"unclear whether the goal protects margin: {unclear!r}. Set the goal's protect list")
    return line


def describe(ref: Mapping) -> str:
    if ref.get("fact_id") not in (None, ""):
        return f"fact {ref['fact_id']}"
    if ref.get("scenario_input"):       # before the goal line, which can carry a pre-filled number
        return f"scenario input: {ref['scenario_input']}"
    if ref.get("goal_line"):
        return f"goal: {ref['goal_line']}"
    return str(ref.get("label") or "")


def cited(refs: list[dict]) -> str:
    facts = [r for r in refs if r.get("fact_id") not in (None, "")]
    shown = list(map(describe, facts[:FACTS_SHOWN]))
    if len(facts) > FACTS_SHOWN:
        shown.append(f"and {len(facts) - FACTS_SHOWN} more facts")
    return "; ".join(filter(None, shown + [describe(r) for r in refs if r not in facts]))


def row(name: str, text: str, given: Input, unit: str = "") -> dict:
    out = {"name": name, "text": text, "value": given.value, "unit": unit, "refs": given.refs,
           "source": cited(given.refs)}
    return {**out, "note": given.note} if given.note else out


def remainder_action(order_value: Input, deposit: Input) -> dict:
    """What is still to pay after the deposit, as a purchase order action of its own."""
    paid = deposit_amount(deposit, order_value)
    left = round(amount(order_value, "order value") - paid.value, 2)
    return _action("remainder", left, "backup order value minus deposit", order_value, paid)


def _action(step: str, value: float, label: str, order_value: Input, *more: Input) -> dict:
    refs = unique([r for given in (order_value, *more) for r in given.refs])
    return {"action": "purchase_order", "step": step, "amount_eur": value,
            "amount_source": {"label": label, "value": value, "inputs": refs},
            "order_value_eur": order_value.value}       # the whole order, for a policy with limits per order


def _breakeven(commitment: float, at_stake: float, order: float, margin: bool) -> float | None:
    """The P(fail) above which the hedge pays. None: no probability does."""
    room = at_stake - (order - commitment if margin else 0)
    if room <= 0 or commitment >= room:
        return None
    return commitment / room


def decide(*, p_fail: Input, copies_at_risk: Input, loss_per_copy: Input, units: Input, unit_price: Input,
           units_per_copy: Input, cost_model: Input, deposit: Input | None = None, salvage: Input | None = None,
           goal: Mapping | None = None) -> dict:
    """The verdict with its numbers and the drill-down. `salvage` is EUR for the whole backup order."""
    p = amount(p_fail, "p_fail", 1)
    at_risk, loss = amount(copies_at_risk, "copies at risk"), amount(loss_per_copy, "loss per copy")
    count, price = positive(units, "backup units"), amount(unit_price, "unit price")
    per_copy = positive(units_per_copy, "units per copy")
    model = cost_model.value
    if model not in COST_MODELS:
        raise ModelError(f"cost model must be one of {', '.join(COST_MODELS)}, got {model!r}")

    inputs = [row("p_fail", "Chance the primary fails", p_fail),
              row("copies_at_risk", "Copies at risk", copies_at_risk, "copies"),
              row("loss_per_copy", "Loss per missing copy", loss_per_copy, "EUR"),
              row("units", "Backup order", units, "units"),
              row("units_per_copy", "Units per copy", units_per_copy, "units"),
              row("unit_price", "Backup price per unit", unit_price, "EUR"),
              row("cost_model", "Cost model", cost_model)]

    total = Input(round(count * price, 2), units.refs + unit_price.refs, "units x unit price", computed=True)
    paid = deposit_amount(deposit, total) if deposit is not None else None
    if paid:
        inputs.append(row("deposit", "Deposit", paid, "EUR"))
    recovered = 0.0
    if model == "deposit_only":
        if not paid:
            raise ModelError("deposit_only needs the deposit")
        if salvage is not None:
            raise ModelError("salvage belongs to the full_order cost model")
        commitment = paid.value
    else:
        if salvage is not None:
            recovered = amount(salvage, "salvage")
            if recovered > total.value:
                raise ModelError("salvage is more than the order")
            inputs.append(row("salvage", "Salvage if the backup is not needed", salvage, "EUR"))
        commitment = round(total.value - recovered, 2)

    covered = min(at_risk, math.floor(round(count / per_copy, 6)))
    at_stake = covered * loss
    avoided = p * at_stake
    said = margin_line(goal)
    cost = commitment + p * (total.value - commitment) if said else commitment
    if said:
        inputs.append(row("protect_margin", "Goal protects margin", Input("yes", {"goal_line": said})))
    # compared to a millionth of a euro: a float rounding error is no reason to spend money
    hedged = round(avoided, 6) > round(cost, 6)
    even = _breakeven(commitment, at_stake, total.value, bool(said))

    def computed(name, text, value, formula, *used, unit="EUR"):
        return {"name": name, "text": text, "value": value, "unit": unit, "source": formula, "from": list(used)}

    committed = ("the deposit, the backup can be cancelled", "deposit") if model == "deposit_only" else \
        (("order value minus salvage", "order_value", "salvage") if salvage is not None
         else ("order value, no salvage given", "order_value"))
    margin = ("protect_margin",) if said else ()
    steps = [
        computed("copies_covered", "Copies the backup protects", covered,
                 "smaller of copies at risk and backup units / units per copy",
                 "copies_at_risk", "units", "units_per_copy", unit="copies"),
        computed("order_value", "Backup order value", total.value, "units x unit price", "units", "unit_price"),
        computed("commitment", "Committed to the backup", commitment, committed[0], *committed[1:]),
        computed("expected_loss", "Expected loss without the backup", round(avoided, 2),
                 "P(fail) x copies x loss per copy", "p_fail", "copies_covered", "loss_per_copy"),
        computed("hedge_cost", "Cost weighed against it", round(cost, 2),
                 "(1 - P(fail)) x commitment + P(fail) x order value" if said else "the commitment",
                 "commitment", *(("p_fail", "order_value") if said else ()), *margin),
        computed("breakeven_p_fail", "Break-even chance of failure", even,
                 "commitment / (copies x loss - order value + commitment)" if said else "commitment / (copies x loss)",
                 "commitment", "copies_covered", "loss_per_copy", *(("order_value",) if said else ()), *margin,
                 unit=""),
    ]
    actions, rest = [], None
    if paid:
        rest = remainder_action(total, paid)
        steps.append(computed("remainder", "Still to pay after the deposit", rest["amount_eur"],
                              "order value minus deposit", "order_value", "deposit"))
    if hedged:
        if paid:
            actions = [_action("deposit", paid.value, "deposit on the backup order", total, paid)]
            if rest["amount_eur"]:
                actions.append(rest)
        else:
            actions = [_action("order", total.value, "backup order value", total)]

    why = (f"Expected loss of {avoided:,.2f} EUR ({p:.0%} x {covered:,} copies x {loss:,} EUR) "
           f"{'exceeds' if hedged else 'does not exceed'} the {cost:,.2f} EUR cost of the backup")
    if said:
        why += ", which includes the order itself because the goal protects margin"
    why += f". Break-even at {even:.2%}." if even is not None else ". No chance of failure would justify it."

    shape = {"kind": "fixed", "fixed_eur": commitment} if model == "deposit_only" else \
        {"kind": "per_unit", "unit_eur": price, "salvage_eur": recovered}
    return {
        "verdict": "hedge" if hedged else "hold", "hedge": hedged, "protect_margin": bool(said),
        "p_fail": p, "copies_at_risk": at_risk, "copies_covered": covered, "loss_per_copy_eur": loss,
        "units": count, "order_value_eur": total.value, "commitment_eur": commitment,
        "cost_model": {**shape, "basis": model},
        "expected_loss_eur": round(avoided, 2), "hedge_cost_eur": round(cost, 2), "breakeven_p_fail": even,
        "remainder": rest, "actions": actions, "rationale": why,
        "drilldown": {"inputs": inputs, "computed": steps},
        "sources": unique([ref for r in inputs for ref in r["refs"]]),
    }


def recommendation(result: Mapping, part_id: str, **fields) -> dict:
    """The `recommendation` contract. `fields` are its plain ones: suppliers, offer, decide_by, run and so on."""
    rows = {r["name"]: r for r in result["drilldown"]["inputs"]}

    def cite(name: str) -> dict:
        refs = rows[name]["refs"]
        return refs[0] if len(refs) == 1 else {"label": rows[name].get("note") or rows[name]["text"], "inputs": refs}

    keys = ("p_fail", "loss_per_copy_eur", "commitment_eur", "cost_model", "copies_at_risk", "copies_covered",
            "expected_loss_eur", "hedge_cost_eur", "breakeven_p_fail")
    return {**fields, "kind": result["verdict"], "part_id": part_id, "quantity": result["units"],
            "hedge": {**{k: result[k] for k in keys}, "p_fail_source": cite("p_fail"),
                      "loss_per_copy_source": cite("loss_per_copy")},
            "actions": result["actions"], "drilldown": result["drilldown"],
            "rationale": result["rationale"], "sources": result["sources"]}
