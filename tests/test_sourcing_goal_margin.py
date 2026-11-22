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


@pytest.mark.parametrize("said", [
    "Do not, under any circumstances, protect margin", "Never, ever, protect margin", "Not a priority: protect margin",
    "Under no circumstances, protect margin", "Protect margin, not", "Protect margin - no", "Forget it: protect margin",
    "If there is money left, protect margin", "Protect margin, if possible", "Protect margin, time permitting",
    "Protect margin, but only after the deadline is safe", "Protect margin and forget it",
    "Protect margin, that can wait",
    "Never, margin can suffer", "It is not true: margin can suffer", "Margin can suffer, if goodwill holds",
    "I would never choose speed rather than protect margin", "Protect margin. Actually no", "Protect margin... not",
    "Don't. Protect margin", "If there is money left; protect margin", "Protect margin; only later",
    "It is not true; margin can suffer",
])
def test_words_elsewhere_in_the_sentence_are_not_dropped(said):
    for goal in ({"statement": said}, {"answers": {"limits": said}}):
        with pytest.raises(ModelError, match="margin"):
            hedge.margin_line(goal)
    assert hedge.margin_line({"statement": said, "protect": ["margin"]})
    assert hedge.margin_line({"statement": said, "protect": []}) is None


@pytest.mark.parametrize("said", [
    "All copies on time, protect margin", "Protect margin, goodwill can suffer", "Don't miss the date, protect margin",
    "All first copies on time, goodwill can suffer, don't bankrupt me, protect margin",
    "A missing copy costs 70 EUR, a late one 20 EUR. Protect margin. Spend at most 900 EUR.",
])
def test_plain_asides_next_to_margin_are_still_read(said):
    assert hedge.margin_line({"statement": said})


@pytest.mark.parametrize("question", [
    "What must we never do?", "What is least important?", "What should we avoid?", "What would you give up?",
])
def test_an_answer_is_not_read_without_a_question_that_turns_it_around(question):
    for answers in ([{"q": question, "a": "Protect margin"}], {question: "Protect margin"}):
        with pytest.raises(ModelError, match="margin"):
            hedge.margin_line({"answers": answers})


@pytest.mark.parametrize("goal", [
    {"protect": {"margin": True}}, {"protect": [{"margin": True}]}, {"sacrifices": [{"margin": "no"}]},
])
def test_margin_named_by_a_key_is_not_dropped(goal):
    with pytest.raises(ModelError, match="margin"):
        hedge.margin_line(goal)


@pytest.mark.parametrize("said", [
    "Protect margin. Only if it is cheap.", "Protect margin. That can wait.", "Protect margin. Those can wait.",
    "Under no circumstances do this. Protect margin.", "Protect margin. Time permitting.", "Protect margin. Later.",
    "Protect margin\nMaybe", "Margin can suffer. Within reason.", "Protect margin. All of the above can wait.",
    "Protect margin. Profit can suffer.", "Later\nAll copies on time\nMargin protection first",
    "All copies on time margin can suffer, maybe",
])
def test_words_in_another_sentence_are_not_dropped(said):
    for goal in ({"statement": said}, {"answers": {"limits": said}}):
        with pytest.raises(ModelError, match="margin"):
            hedge.margin_line(goal)
    assert hedge.margin_line({"statement": said, "protect": ["margin"]})


@pytest.mark.parametrize("goal", [
    {"answers": {"goal": "Protect margin", "limits": "Only if it is cheap"}},
    {"statement": "Protect margin", "answers": [{"id": "limits", "q": "How far will you go?", "a": "Maybe later"}]},
])
def test_words_in_another_answer_are_not_dropped(goal):
    with pytest.raises(ModelError, match="margin"):
        hedge.margin_line(goal)


@pytest.mark.parametrize("limits", [
    "A missing copy costs 70 EUR. Spend at most 1,000 EUR. Goodwill can suffer.",
    "One missing copy is worth 70.50 EUR, don't bankrupt me, the team can be asked until Sunday 10:00.",
    "Each missing copy costs €70; spend up to €900. The team is offline from 10:00.",
])
def test_an_interview_goal_on_margin_is_still_read(limits):
    said = "All copies on time. Protect margin."
    stored = [{"id": "goal", "q": "What has to be true at the deadline?", "a": said},
              {"id": "limits", "q": "How far will you go?", "a": limits},
              {"id": "team_window", "a": {"to": "2031-05-04T10:00:00+02:00"}}]
    for answers in ({"goal": said, "limits": limits}, stored):
        assert hedge.margin_line({"statement": said, "answers": answers})
