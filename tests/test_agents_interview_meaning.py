"""A quote that checks out word for word and still means something else must stay an open point.
Synthetic editions and numbers; every model answer is a replay, repeated for the two retries."""

import json
from datetime import datetime, timezone

import pytest

from agents import interview as iv
from agents import interview_store as store
from agents import llm

DELUXE, BASIC = EDITIONS = ["Deluxe Edition", "Basic Edition"]
ZONE = "Europe/Berlin"
NOW = datetime(2030, 1, 12, 17, 0, tzinfo=timezone.utc)      # a Saturday, 18:00 in Berlin
SUNDAY = "2030-01-13T10:00:00+01:00"
INPUTS = {"label": "pre-fill", "unit_loss": "60", "budget": "700"}


def item(field, quote, amount="", edition=""):
    return {"field": field, "quote": quote, "amount": amount, "edition": edition}


def read(answer, *items, inputs=INPUTS, editions=EDITIONS):
    client = llm.ReplayClient([{"text": json.dumps({"items": list(items)})}] * 3)
    return iv.interview(client, answer, editions=editions, inputs=inputs, now=NOW, zone=ZONE)


def blocked(draft):
    assert draft.problems and not draft.ready, draft.show()


def confirmed(draft):
    assert draft.ready, draft.problems
    return store.build(draft.answers, draft.lines)


# ---- A: an exception names the edition that is left out ------------------------------

@pytest.mark.parametrize("field, answer, amount", [
    ("priority", "All except Deluxe on time", ""),
    ("priority", "Everything but Deluxe on time", ""),
    ("priority", "All editions other than Deluxe on time", ""),
    ("value", "75 EUR for anything but Deluxe", "75 EUR"),
])
def test_an_excepted_edition_is_not_the_one_meant(field, answer, amount):
    blocked(read(answer, item(field, answer, amount, DELUXE)))


# ---- B: a trade-off is read back with its whole clause --------------------------------

@pytest.mark.parametrize("answer, quote", [
    ("Goodwill can suffer over my dead body", "Goodwill can suffer"),
    ("It is false that goodwill can suffer", "goodwill can suffer"),
    ("I would be upset if goodwill may suffer", "goodwill may suffer"),
    ("It is wrong to let goodwill suffer", "let goodwill suffer"),
    ("Goodwill can suffer only as a final resort", "Goodwill can suffer"),
])
def test_a_cropped_trade_off_blocks(answer, quote):
    blocked(read(answer, item("trade_off", quote)))


@pytest.mark.parametrize("answer, quote", [
    ("On time. And supplier goodwill can suffer.", "supplier goodwill can suffer"),
    ("Don't bankrupt me, but margin may suffer", "margin may suffer"),
    ("Margin may suffer, quality must not", "Margin may suffer"),
    ('"Margin can suffer".', "Margin can suffer"),
])
def test_a_whole_clause_is_a_trade_off(answer, quote):
    assert confirmed(read(answer, item("trade_off", quote)))["sacrifices"] == [quote]


# ---- C: words that set a limit or a time are not replaced by the pre-fill ----------------

@pytest.mark.parametrize("answer", ["Unlimited budget", "No spend limit", "Budget is whatever it takes",
                                    "Budget is nil"])
def test_a_limit_in_words_is_not_replaced_by_the_prefill(answer):
    blocked(read(answer))
    blocked(read(answer, item("ceiling", answer)))


@pytest.mark.parametrize("answer", ["Team offline at dawn", "Team offline at dusk", "Team offline in a few hours",
                                    "Team is offline now", "Team offline all weekend"])
def test_a_team_time_in_words_is_not_replaced_by_the_prefill(answer):
    blocked(read(answer, inputs={**INPUTS, "team_window": SUNDAY}))


# ---- D: a ceiling is a total, a value is per copy, neither is a lower bound ----------------

@pytest.mark.parametrize("said, number", [
    ("500 EUR per day", "500 EUR"), ("500 per day", "500"), ("5 EUR per copy", "5 EUR"), ("500 each", "500"),
    ("€500 per order", "€500"), ("500 euros a supplier", "500 euros"), ("500 EUR a week", "500 EUR"),
    ("500 EUR/order", "500 EUR"), ("500 for each supplier", "500"), ("500 EUR daily", "500 EUR"),
    ("500 EUR or more", "500 EUR"), ("500 EUR plus VAT", "500 EUR"),
])
def test_a_rate_is_no_ceiling(said, number):
    answer = f"Spend at most {said}"
    blocked(read(answer, item("ceiling", answer, number)))
    blocked(read(answer, item("ceiling", number, number)))


@pytest.mark.parametrize("answer", ["Budget at least 500 EUR", "Budget is more than 500 EUR",
                                    "Budget of a minimum of 500 EUR"])
def test_a_lower_bound_is_no_ceiling(answer):
    blocked(read(answer, item("ceiling", answer, "500 EUR")))


@pytest.mark.parametrize("answer, number", [
    ("75 EUR per pallet", "75 EUR"), ("75 EUR per box", "75 EUR"), ("75 EUR/carton", "75 EUR"),
    ("A missing pallet costs 75 EUR", "75 EUR"), ("A missing box costs 75 EUR", "75 EUR"),
    ("A missing copy costs 75 EUR per day", "75 EUR"), ("A missing copy costs 75 a day", "75"),
    ("A missing copy costs more than 75 EUR", "75 EUR"),
])
def test_another_unit_is_no_value_per_copy(answer, number):
    blocked(read(answer, item("value", answer, number)))


@pytest.mark.parametrize("field, answer, number", [
    ("ceiling", "Spend no more than 500 EUR", "500 EUR"),
    ("ceiling", "Spend at most 500 EUR in total", "500 EUR"),
    ("ceiling", "Spend not more than 500", "500"),
    ("value", "75 EUR per missing copy", "75 EUR"),
    ("value", "75 a copy", "75"),
    ("value", "75 EUR/copy", "75 EUR"),
    ("value", "A missing copy costs 75 EUR each", "75 EUR"),
])
def test_a_total_and_a_value_per_copy_still_confirm(field, answer, number):
    goal = confirmed(read(answer, item(field, answer, number)))
    assert goal["budget_cap_eur" if field == "ceiling" else "unit_loss_eur"] in (75, 500)


def test_an_edition_named_like_a_pack_still_has_a_value():
    box = "Box Edition"
    answer = "A missing Box copy costs 75 EUR"
    goal = confirmed(read(answer, item("value", answer, "75 EUR", box), editions=[box, BASIC],
                          inputs={"label": "pre-fill", "budget": "700"}))
    assert (goal["edition"], goal["unit_loss_eur"]) == (box, 75)


# ---- E: the side of the team window and the time itself -------------------------------

@pytest.mark.parametrize("answer, field", [
    ("Team available from Sun 10:00", "team_until"), ("Team available until Sun 10:00", "team_from"),
    ("Team online till Sun 10:00", "team_from"), ("Team offline from Sun 10:00", "team_from"),
    ("Team offline until Sun 10:00", "team_until"), ("Team asleep till Sun 10:00", "team_until"),
])
def test_the_wrong_side_of_the_window_blocks(answer, field):
    blocked(read(answer, item(field, "Sun 10:00")))


@pytest.mark.parametrize("answer, field, side", [
    ("Team available from Sun 10:00", "team_from", "from"), ("Team available until Sun 10:00", "team_until", "to"),
    ("Team offline from Sun 10:00", "team_until", "to"), ("Team offline until Sun 10:00", "team_from", "from"),
    ("No calls from Sun 10:00", "team_until", "to"),
])
def test_the_right_side_of_the_window_confirms(answer, field, side):
    assert confirmed(read(answer, item(field, "Sun 10:00")))["team_window"] == {side: SUNDAY}


@pytest.mark.parametrize("said", ["an hour before 10:00", "shortly after 10:00", "a few minutes before 10:00",
                                  "around sunset, 8:00", "at dusk, 8:00", "10:00 the day of launch"])
def test_a_shifted_or_unclear_time_blocks(said):
    quote = "8:00" if "8:00" in said else "10:00"
    blocked(read(f"Team offline {said}", item("team_until", quote)))


def test_a_fortnight_is_no_next_sunday():
    blocked(read("Team offline a fortnight on Sunday 10:00", item("team_until", "Sunday 10:00")))


@pytest.mark.parametrize("quote", ["Supplier goodwill can suffer", "Supplier goodwill can suffer."])
def test_a_full_stop_in_the_quote_does_not_borrow_the_next_refusal(quote):
    answer = "All on time. Supplier goodwill can suffer. Don't bankrupt me."
    assert confirmed(read(answer, item("trade_off", quote), item("ceiling", "Don't bankrupt me")))["sacrifices"]
