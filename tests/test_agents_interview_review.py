"""Answers the goal interview must not turn into a different goal, and plain answers it must still accept.
Synthetic editions and numbers; every model answer is a replay, repeated for the two retries."""

import json
from datetime import datetime, timezone

import pytest

from agents import interview as iv
from agents import interview_store as store
from agents import llm
from plan import contracts

DELUXE, BASIC = EDITIONS = ["Deluxe Edition", "Basic Edition"]
ZONE = "Europe/Berlin"
NOW = datetime(2030, 1, 12, 17, 0, tzinfo=timezone.utc)      # a Saturday, 18:00 in Berlin
SUNDAY = "2030-01-13T10:00:00+01:00"
INPUTS = {"label": "pre-fill", "unit_loss": "60", "budget": "700"}
GOODWILL = {**INPUTS, "trade_offs": ["Supplier goodwill can suffer"]}


def item(field, quote, amount="", edition=""):
    return {"field": field, "quote": quote, "amount": amount, "edition": edition}


def read(answers, *items, inputs=INPUTS):
    client = llm.ReplayClient([{"text": json.dumps({"items": list(items)})}] * 3)
    return iv.interview(client, answers, editions=EDITIONS, inputs=inputs, now=NOW, zone=ZONE)


def goal_of(draft):
    return store.build(draft.answers, draft.lines)


def blocked(draft):
    assert draft.problems and not draft.ready, draft.show()


# ---- the edition of a value is the one named where the amount is stated ----------

PRICES = "A missing Deluxe copy costs 75 EUR, a Basic one 20 EUR"


@pytest.mark.parametrize("items", [
    [item("value", "A missing Deluxe copy costs 75 EUR", "75 EUR", BASIC),
     item("value", "a Basic one 20 EUR", "20 EUR", DELUXE)],
    [item("value", PRICES, "75 EUR", BASIC), item("value", PRICES, "20 EUR", DELUXE)],
])
def test_swapped_editions_block(items):
    blocked(read(PRICES, *items))


@pytest.mark.parametrize("answer, items", [
    ("All Deluxe on time", [item("priority", "All Deluxe on time", edition="all")]),
    ("All Deluxe on time", [item("priority", "All Deluxe on time")]),
    ("All Deluxe on time", [item("priority", "on time", edition="all")]),
    ("All Deluxe on time", []),
    ("All Deluxe and Basic on time", [item("priority", "All Deluxe and Basic on time", edition=DELUXE)]),
    ("A missing Deluxe copy costs 75 EUR", [item("value", "A missing Deluxe copy costs 75 EUR", "75 EUR", "all")]),
    ("A missing Deluxe copy costs 75 EUR", [item("value", "75 EUR", "75 EUR")]),
    ("Deluxe matters, 75 EUR each", [item("value", "75 EUR each", "75 EUR", "all")]),
])
def test_one_edition_is_not_all_editions(answer, items):
    blocked(read(answer, *items))


# ---- a refusal is not a trade-off ------------------------------------------------

@pytest.mark.parametrize("answer, quote", [
    ("Never cut quality.", "cut quality"),
    ("Never let quality and goodwill suffer.", "goodwill suffer"),
    ("Never, ever, let goodwill suffer.", "let goodwill suffer"),
    ("Supplier goodwill, of course, must not suffer.", "Supplier goodwill"),
    ("Goodwill must be protected.", "Goodwill must be protected"),
    ("Never cut quality\nor let margin suffer", "margin suffer"),
    *[(said, said) for said in (
        "Supplier goodwill should not suffer", "Supplier goodwill shouldn't suffer",
        "Supplier goodwill shouldn’t suffer", "Supplier goodwill mustn't suffer",
        "Supplier goodwill is not allowed to suffer", "No supplier goodwill may suffer")],
])
def test_a_refusal_is_no_trade_off(answer, quote):
    blocked(read(answer, item("trade_off", quote)))


@pytest.mark.parametrize("answer", [
    "Supplier goodwill must not suffer", "Keep supplier goodwill intact", "Nothing may suffer"])
def test_a_typed_refusal_is_not_replaced_by_the_prefilled_trade_off(answer):
    blocked(read(answer, inputs=GOODWILL))


# ---- a typed figure is never replaced by the pre-fill ------------------------------

TWICE = "A copy is worth 100 EUR. Spend at most 100 EUR."


@pytest.mark.parametrize("answer, items", [
    ("Spend at most five thousand euros", [item("ceiling", "Spend at most five thousand euros")]),
    ("Spend at most five thousand euros", []),
    ("A missing copy costs ninety euros", []),
    ("A missing copy costs ninety", []),
    ("Spend nothing", [item("ceiling", "Spend nothing")]),
    (TWICE, [item("value", "100 EUR", "100 EUR")]),
    (TWICE, [item("value", "A copy is worth 100 EUR", "100 EUR")]),
    (TWICE, [item("value", "Spend at most 100 EUR", "100 EUR")]),
    ("A copy is worth 30 EUR and spend at most 500",
     [item("value", "A copy is worth 30 EUR and spend at most 500", "30 EUR")]),
    ("A copy is worth 30 EUR and spend at most 500",
     [item("value", "A copy is worth 30 EUR and spend at most 500", "500")]),
    ("All on time, spend at most 4321", [item("priority", "All on time, spend at most 4321")]),
    ("All on time, 4321 tops", [item("priority", "All on time, 4321 tops")]),
])
def test_a_typed_figure_is_not_replaced_by_the_prefill(answer, items):
    blocked(read(answer, *items))


@pytest.mark.parametrize("answer, items", [
    ("All on time, team offline Sun 10:00", [item("priority", "All on time, team offline Sun 10:00")]),
    ("Team offline at ten tomorrow", []),
    ("Team offline Sunday morning", []),
])
def test_a_typed_team_time_is_not_replaced_by_the_prefill(answer, items):
    blocked(read(answer, *items, inputs={**INPUTS, "team_window": SUNDAY}))


# ---- no amount is cut out of its unit or currency ----------------------------------

@pytest.mark.parametrize("said, number", [
    ("5 grand", "5"), ("5 Tausend", "5"), ("2 millions", "2"), ("2 Millionen", "2"), ("3 mln", "3"),
    ("5 lakh", "5"), ("€5 grand", "€5"), ("500 dollars", "500"), ("500 CHF", "500"), ("500 pounds", "500"),
    ("CHF 500", "500"), ("$ 500", "500"), ("500, in dollars", "500"), ("500 in Swiss francs", "500"),
])
def test_an_amount_is_not_cut_out_of_its_unit(said, number):
    answer = f"Spend at most {said}"
    blocked(read(answer, item("ceiling", answer, number)))
    blocked(read(answer, item("ceiling", number, number)))


# ---- the team window is what was said, or an open point ----------------------------

@pytest.mark.parametrize("said, quote", [
    ("9:00 in the evening", "9:00"), ("9:00 in the evening", "9:00 in the evening"),
    ("tonight 9:00", "tonight 9:00"), ("today 8:00", "today 8:00"),
    ("day after tomorrow 10:00", "day after tomorrow 10:00"), ("day after tomorrow 10:00", "tomorrow 10:00"),
    ("10:00 UTC", "10:00 UTC"), ("10:00 UTC", "10:00"), ("10:00Z", "10:00"), ("10:00 London time", "10:00"),
    ("10:00 in London", "10:00"), ("Sunday, 10:00", "10:00"), ("next Sunday 10:00", "Sunday 10:00"),
])
def test_an_unclear_team_time_blocks(said, quote):
    blocked(read(f"Team offline {said}", item("team_until", quote)))


@pytest.mark.parametrize("said, quote, at", [
    ("9:00 p.m.", "9:00 p.m.", "2030-01-12T21:00:00+01:00"),
    ("9:00 p.m.", "9:00", "2030-01-12T21:00:00+01:00"),
    ("10:00 pm", "10:00", "2030-01-12T22:00:00+01:00"),
    ("Sun 10:00 a.m.", "Sun 10:00 a.m.", SUNDAY),
    ("Sun 10:00 in the morning", "Sun 10:00", SUNDAY),
])
def test_am_and_pm_are_read_from_the_answer(said, quote, at):
    draft = read(f"Team offline {said}", item("team_until", quote))
    assert draft.ready, draft.problems
    assert goal_of(draft)["team_window"] == {"to": at}


# ---- saying it twice is fine, saying two different things is not -------------------

def test_the_same_value_twice_can_be_confirmed():
    draft = read({"goal": "All on time, 75 EUR a copy", "limits": "A missing copy costs 75 EUR"},
                 item("priority", "All on time"), item("value", "75 EUR a copy", "75 EUR"),
                 item("value", "A missing copy costs 75 EUR", "75 EUR"))
    assert draft.ready, draft.problems
    assert goal_of(draft)["unit_loss_eur"] == 75
    assert len([x for x in draft.lines if x["field"] == "value"]) == 1


@pytest.mark.parametrize("second", ["Team offline Sun 10:00", "Sun 10:00"])
def test_the_same_time_twice_can_be_confirmed(second):
    draft = read({"goal": "All on time. Team offline Sun 10:00", "limits": second},
                 item("team_until", "Sun 10:00"), item("team_until", "Sun 10:00"))
    assert draft.ready, draft.problems
    assert goal_of(draft)["team_window"] == {"to": SUNDAY}


@pytest.mark.parametrize("answer, first, second", [
    ("30 EUR a copy. 50 EUR a copy.", "", "all"),
    ("30 EUR a copy. 50 EUR a copy.", "all", ""),
    ("30 EUR a copy. 50 EUR a copy for Deluxe.", "", DELUXE),
])
def test_two_different_values_conflict(answer, first, second):
    items = [item("value", "30 EUR a copy", "30 EUR", first), item("value", "50 EUR a copy", "50 EUR", second)]
    if second == DELUXE:
        answer = f"All Deluxe on time. {answer}"
        items.append(item("priority", "All Deluxe on time", edition=DELUXE))
    blocked(read(answer, *items))


# ---- plain answers still confirm ---------------------------------------------------

@pytest.mark.parametrize("answer, items, value, cap", [
    ("All 40 Deluxe copies on time. 75 a copy. Spend at most 500.",
     [item("priority", "All 40 Deluxe copies on time", edition=DELUXE), item("value", "75 a copy", "75"),
      item("ceiling", "Spend at most 500", "500")], 75, 500),
    ("All Deluxe on time, 75 EUR a copy",
     [item("priority", "All Deluxe on time", edition=DELUXE), item("value", "75 EUR a copy", "75 EUR")], 75, 700),
    ("Everything on time by Sun 20:00. €5k max.",
     [item("priority", "Everything on time by Sun 20:00", edition="all"), item("ceiling", "€5k max", "€5k")],
     60, 5000),
    ("All on time. Don't bankrupt me, but supplier goodwill can suffer.",
     [item("priority", "All on time"), item("ceiling", "Don't bankrupt me"),
      item("trade_off", "supplier goodwill can suffer")], 60, 700),
    ("All Deluxe on time, Basic doesn't matter",
     [item("priority", "All Deluxe on time", edition=DELUXE), item("trade_off", "Basic doesn't matter")], 60, 700),
    ("A copy is worth 100 EUR. Spend at most 100 EUR.",
     [item("value", "100 EUR", "100 EUR"), item("ceiling", "Spend at most 100 EUR", "100 EUR")], 100, 100),
])
def test_plain_answers_still_confirm(answer, items, value, cap):
    draft = read(answer, *items)
    assert draft.ready, draft.problems
    goal = goal_of(draft)
    assert (goal["unit_loss_eur"], goal["budget_cap_eur"]) == (value, cap)


def test_a_prefilled_trade_off_still_applies_when_nothing_contradicts_it():
    draft = read("All on time. Don't bankrupt me.", item("priority", "All on time"),
                 item("ceiling", "Don't bankrupt me"), inputs=GOODWILL)
    assert draft.ready, draft.problems
    assert goal_of(draft)["sacrifices"] == GOODWILL["trade_offs"]


def test_an_iso_deadline_does_not_hide_the_team_time():
    deadline = "On time by 2030-01-13T20:00:00+01:00"
    draft = read(f"{deadline}. Team until Sun 10:00", item("priority", deadline), item("team_until", "Sun 10:00"))
    assert draft.ready, draft.problems
    assert goal_of(draft)["team_window"] == {"to": SUNDAY}


# ---- who confirms, what can be cited -----------------------------------------------

@pytest.mark.parametrize("author", ["goal_interview", "Goal interview", "monitor", "hedger", "bot", "Coordinator"])
def test_a_role_is_not_a_person(author):
    assert iv.NOT_A_PERSON.search(author)


@pytest.mark.parametrize("author", ["Monika Example", "Bo Botha", "Test Person"])
def test_a_person_is_a_person(author):
    assert not iv.NOT_A_PERSON.search(author)


def test_priority_and_trade_off_lines_can_be_cited():
    draft = read("All Deluxe on time. Basic can wait.", item("priority", "All Deluxe on time", edition=DELUXE),
                 item("trade_off", "Basic can wait"))
    goal = goal_of(draft)
    for source in (store.cite(goal, "priority"), store.cite(goal, "priority", DELUXE),
                   store.cite(goal, "trade_off")):
        assert source["goal_line"] and "value" not in source
        contracts.validate("recommendation", {
            "kind": "hedge", "part_id": "PART-A", "rationale": "x", "sources": [source], "hedge": {
            "p_fail": 0.1, "loss_per_copy_eur": 1, "loss_per_copy_source": source, "commitment_eur": 1,
            "cost_model": {"kind": "fixed", "fixed_eur": 1}}})


def test_prefilled_trade_offs_are_a_list():
    with pytest.raises(iv.InterviewError):
        read("All on time", inputs={"label": "x", "trade_offs": "margin"})
