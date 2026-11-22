"""How a goal's words on margin are read (sourcing/goal_margin.py). Every value here is synthetic."""

import pytest

from sourcing import hedge
from sourcing.hedge import ModelError

MARGIN = {"statement": "All copies on time. Protect margin."}


@pytest.mark.parametrize("goal", [
    MARGIN, {"statement": "protect our margins"}, {"protect": ["margin"]}, {"answers": {"sacrifice": "Protect margin"}},
    {"answers": [{"id": "limits", "a": "On time, but preserve margin"}]},
])
def test_goal_that_protects_margin(goal):
    assert hedge.margin_line(goal)


@pytest.mark.parametrize("goal", [
    None, {}, {"statement": "All on time. Goodwill can suffer. Don't bankrupt me."},
    {"statement": "Don't protect margin"}, {"statement": "No need to protect margin"},
    {"statement": "Protect margin", "sacrifices": ["margin"]},
    {"answers": [{"q": "Should we protect margin?", "a": "no"}]},
])
def test_goal_that_does_not_protect_margin(goal):
    assert hedge.margin_line(goal) is None


@pytest.mark.parametrize("said", [
    "Protect margin and don't bankrupt me", "Protect margin and do not overspend",
    "Protect margin - don't bankrupt me", "Margin protection first", "Our margin must be protected",
    "Protect our profit margin", "Don't sacrifice margin", "Margin can't suffer",
])
def test_margin_is_protected_in_common_wordings(said):
    assert hedge.margin_line({"statement": said})


@pytest.mark.parametrize("said", [
    "We cannot protect margin", "Stop protecting margin", "Forget protecting margin",
    "Protection of margin can wait", "All on time rather than protect margin", "Margin can suffer",
    "We don't care about margin", "Margins don't matter", "Keep a safety margin of two days",
])
def test_margin_is_not_protected_when_the_goal_says_the_opposite(said):
    assert hedge.margin_line({"statement": said}) is None


@pytest.mark.parametrize("goal", [
    {"statement": "Margin"}, {"statement": "Do not forget about protecting margin"},
    {"statement": "Protect margin unless copies are at risk"}, {"statement": "Protect margin. Margin can suffer"},
    {"sacrifices": ["anything but margin"]}, {"statement": "Anything but margin can suffer"},
    {"statement": "We cannot hit the date and protect margin"},
])
def test_an_unclear_word_on_margin_is_refused_not_guessed(goal):
    with pytest.raises(ModelError, match="margin"):
        hedge.margin_line(goal)


@pytest.mark.parametrize("said", [
    "never margin", "no margin", "nothing that hurts margin", "margin is untouchable", "no loss of margin",
    "anything that leaves margin intact", "goodwill, never margin", "margin must be protected",
    "margin and quality must be protected",
])
def test_a_sacrifice_that_keeps_margin_is_not_read_as_giving_it_up(said):
    with pytest.raises(ModelError, match="margin"):
        hedge.margin_line({"statement": "All on time", "sacrifices": [said]})
    assert hedge.margin_line({"sacrifices": [said], "protect": ["margin"]})


@pytest.mark.parametrize("said", ["margin", "some margin", "lower profit margin", "Margin can suffer."])
def test_a_plain_sacrifice_of_margin_wins(said):
    assert hedge.margin_line({"statement": "Protect margin", "sacrifices": [said]}) is None


@pytest.mark.parametrize("said", ["not margin", "everything except margin", "margin does not matter"])
def test_a_protect_list_that_negates_margin_is_refused(said):
    with pytest.raises(ModelError, match="margin"):
        hedge.margin_line({"protect": [said]})


def test_a_safety_margin_in_the_protect_list_is_not_margin():
    assert hedge.margin_line({"protect": ["safety margin"]}) is None


@pytest.mark.parametrize("answers", [
    [{"q": "Should we protect margin?", "a": "yes"}], [{"question": "Should we protect margin?", "a": "Absolutely"}],
    [{"q": "Can margin suffer?", "a": "no"}], {"protect_margin": "yes"},
])
def test_an_answer_to_a_question_on_margin_is_not_dropped(answers):
    with pytest.raises(ModelError, match="margin"):
        hedge.margin_line({"answers": answers})
