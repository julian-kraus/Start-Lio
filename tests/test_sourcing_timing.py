"""Timing chain (sourcing/timing.py). Every value here is synthetic."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from plan import contracts
from sourcing import timing
from sourcing.timing import Chain, Leg, TimingError, Window

ZONE = timezone(timedelta(hours=1))
EVE = datetime(2030, 3, 2, 18, 0, tzinfo=ZONE)          # a Saturday evening
MORNING = datetime(2030, 3, 3, 6, 0, tzinfo=ZONE)       # the cancellation
DEADLINE = datetime(2030, 3, 3, 20, 0, tzinfo=ZONE)
OFFLINE = datetime(2030, 3, 3, 10, 0, tzinfo=ZONE)
NEED = {"scenario_input": "needed at"}
OFFICE = {"timezone": "Europe/Berlin", "days": ["mon", "tue", "wed", "thu", "fri"], "from": "09:00", "to": "17:00",
          "source": {"scenario_input": "office hours"}}


def fact(route, attribute, value, fid=None):
    return {"fact_type": "route", "subject": {"route_id": route}, "attribute": attribute, "value": value,
            "fact_id": fid or f"{route}:{attribute}"}


FACTS = [fact("long", "origin", "factory"), fact("long", "destination", "harbour"), fact("long", "transit_days", 3),
         fact("short", "origin", "harbour"), fact("short", "destination", "warehouse"),
         fact("short", "transit_days", "0.5"),
         fact("direct", "origin", "plant"), fact("direct", "destination", "warehouse"),
         fact("direct", "transit_days", 1),
         {"fact_type": "site", "subject": {"site_id": "warehouse"}, "attribute": "name", "value": "W", "fact_id": "s"}]


def chain(load=4.0, transit=9.0, last_mile=1.0, receiving=1.0):
    return Chain(load=Leg("load", load, {"fact_id": "promise"}),
                 transit=Leg("transit", transit, {"fact_id": "offer"}),
                 last_mile=Leg("last mile", last_mile, {"fact_id": "road"}),
                 receiving=Leg("receiving", receiving, {"scenario_input": "receiving"}))


def assess(c=None, now=EVE, needed=DEADLINE, **kw):
    return timing.assess(c or chain(), now=now, needed_at=needed, needed_source=NEED, **kw)


def at(text):
    return datetime.fromisoformat(text)


# lead time

@pytest.mark.parametrize("moved", ["load", "transit", "last_mile", "receiving"])
def test_lead_time_is_the_sum_of_the_four_legs(moved):
    base = chain(2, 3, 5, 7)
    assert base.lead_time_h == 17
    longer = replace(base, **{moved: replace(getattr(base, moved), hours=getattr(base, moved).hours + 11)})
    assert longer.lead_time_h == 28


def test_lead_time_from_route_facts():
    c = Chain(load=timing.offer_leg("load", "within 4 h", {"fact_id": "statement"}),
              transit=timing.route_leg(FACTS, "long"),
              last_mile=timing.last_mile_leg(FACTS, "long", "warehouse"),
              receiving=Leg("receiving", 2, {"scenario_input": "receiving"}))
    assert c.lead_time_h == 4 + 72 + 12 + 2
    assert c.sources == [{"fact_id": "statement"}, {"fact_id": "long:transit_days"},
                         {"fact_id": "short:transit_days"}, {"scenario_input": "receiving"}]


def test_lead_time_from_offer_text():
    src = {"fact_id": "offer-text"}
    c = replace(chain(), load=timing.offer_leg("load", "within 4 h", src),
                transit=timing.offer_leg("transit", "about 2,5 Std.", src))
    assert c.lead_time_h == 4 + 2.5 + 1 + 1


def test_route_that_ends_at_the_site_has_no_last_mile():
    assert timing.last_mile_leg(FACTS, "direct", "warehouse") == Leg("last mile", 0, {"fact_id": "direct:destination"})


def test_differing_route_facts_count_at_the_slower_and_cite_all():
    leg = timing.route_leg(FACTS + [fact("long", "transit_days", 5, "revised")], "long")
    assert leg.hours == 120
    assert leg.source["fact_ids"] == ["long:transit_days", "revised"]


def test_slowest_of_several_last_mile_routes_counts():
    other = [fact("slow", "origin", "harbour"), fact("slow", "destination", "warehouse"),
             fact("slow", "transit_days", 2)]
    assert timing.last_mile_leg(FACTS + other, "long", "warehouse").hours == 48


@pytest.mark.parametrize("call", [
    lambda: timing.route_leg(FACTS, "absent"),
    lambda: timing.route_leg([fact("r", "transit_days", "soon")], "r"),
    lambda: timing.route_leg([fact("r", "transit_days", None)], "r"),
    lambda: timing.route_leg([dict(fact("r", "transit_days", 1), fact_id=None)], "r"),
    lambda: timing.last_mile_leg(FACTS, "absent", "warehouse"),
    lambda: timing.last_mile_leg(FACTS, "long", "elsewhere"),
    lambda: timing.last_mile_leg(FACTS + [fact("long", "destination", "airport", "x")], "long", "warehouse"),
])
def test_missing_or_unclear_route_facts_are_not_guessed(call):
    with pytest.raises(TimingError):
        call()


@pytest.mark.parametrize("text,hours", [
    ("4 h", 4), ("4h", 4), ("within 4 h", 4), ("Within 4 hours", 4), ("ca. 9 h", 9), ("~9h", 9), ("90 min", 1.5),
    ("1 h 30 min", 1.5), ("2.5 h", 2.5), ("2,5 Std.", 2.5), ("innerhalb von 4 Stunden", 4), ("w ciągu 4 godzin", 4), ("in 4 h", 4), ("max. 4 h", 4),
    ("2 days", 48), ("1 Tag", 24), ("8-10 h", 10), ("8 to 10 hours", 10), ("1 000 min", 1000 / 60),
    ("1 000 min", 1000 / 60),
])
def test_offer_durations_are_parsed_in_code(text, hours):
    assert timing.parse_hours(text) == pytest.approx(hours)


@pytest.mark.parametrize("text", [
    "", "4", "soon", "tomorrow", "2 working days", "4 h after payment", "not within 4 h", "-4 h", "10-8 h",
    "1,000 h", "4 parsecs", None, 4,
])
def test_unclear_durations_are_refused(text):
    with pytest.raises(TimingError):
        timing.parse_hours(text)


@pytest.mark.parametrize("hours,source", [(-1, "s"), (float("nan"), "s"), (float("inf"), "s"), (True, "s"), (10 ** 9, "s"),
                                          ("4", "s"), (4, ""), (4, {}), (4, None)])
def test_a_leg_needs_real_hours_and_a_source(hours, source):
    with pytest.raises(TimingError):
        Leg("load", hours, source)


def test_a_chain_has_no_default_legs():
    with pytest.raises(TypeError):
        Chain(load=Leg("load", 4, "s"), transit=Leg("transit", 9, "s"))


def test_load_promise_given_as_a_time():
    assert timing.load_by_leg(EVE + timedelta(hours=7), EVE, "statement").hours == 7
    with pytest.raises(TimingError):
        timing.load_by_leg(EVE, MORNING, "statement")


# decide_by, too_late_at

@pytest.mark.parametrize("legs", [(4, 9, 1, 1), (0, 0, 0, 0), (0.25, 2.5, 0, 1 / 3), (48, 720, 24, 6)])
def test_decide_by_is_needed_at_minus_lead_time(legs):
    c = chain(*legs)
    decide = timing.decide_by(c, DEADLINE)
    assert decide == DEADLINE - c.lead_time
    assert timing.arrival(c, decide) == DEADLINE


def test_too_late_at_is_the_last_start_that_lands_in_time():
    limit = timing.too_late_at(chain(), DEADLINE)
    assert limit == DEADLINE - timedelta(hours=15)
    on_the_dot, after = assess(now=limit), assess(now=limit + timedelta(seconds=1))
    assert (on_the_dot["too_late"], on_the_dot["ghost"]["in_time"]) == (False, True)
    assert (after["too_late"], after["ghost"]["in_time"]) == (True, False)


def test_offer_expiry_moves_too_late_at_earlier_but_not_decide_by():
    until = EVE + timedelta(hours=2)
    out = assess(now=until + timedelta(minutes=1), valid_until=until, valid_until_source={"fact_id": "validity"})
    assert at(out["too_late_at"]) == until
    assert at(out["decide_by"]) == DEADLINE - timedelta(hours=15)
    assert out["too_late"] and out["ghost"]["offer_expired"] and not out["ghost"]["in_time"]
    assert {"fact_id": "validity"} in out["sources"]
    assert timing.too_late_at(chain(), DEADLINE, DEADLINE) == timing.decide_by(chain(), DEADLINE)


def test_elapsed_hours_hold_across_a_dst_change():
    berlin = ZoneInfo("Europe/Berlin")
    start = datetime(2030, 3, 30, 18, 0, tzinfo=berlin)     # the clocks go forward that night
    landed = timing.arrival(chain(), start)
    assert landed.astimezone(timezone.utc) - start.astimezone(timezone.utc) == timedelta(hours=15)
    assert landed.hour == 10
    assert timing.decide_by(chain(), landed) == start


# team window, office hours

@pytest.mark.parametrize("goal", [
    {"team_window": {"to": OFFLINE.isoformat()}},
    {"answers": {"team_window": OFFLINE.isoformat()}},
    {"answers": [{"id": "goal", "a": "x"}, {"id": "team_window", "a": {"to": OFFLINE.isoformat()}}]},
])
def test_team_window_is_read_from_the_goal(goal):
    contracts.validate_record({"kind": "goal", **goal})
    window = timing.team_window(goal)
    assert (window.start, window.end) == (None, OFFLINE)
    assert window.source["goal_line"] == "team_window"


@pytest.mark.parametrize("goal", [{}, {"answers": {}}, {"answers": ["free text"]}, {"team_window": ""}])
def test_goal_without_a_team_window(goal):
    assert timing.team_window(goal) is None
    assert assess(team=timing.team_window(goal))["act_by"] is None


@pytest.mark.parametrize("value", [{}, {"to": "Sunday ten"}, "2030-03-03T10:00:00", 7,
                                   {"from": DEADLINE.isoformat(), "to": EVE.isoformat()}])
def test_unclear_team_window_is_refused(value):
    with pytest.raises(TimingError):
        timing.team_window({"team_window": value})


@pytest.mark.parametrize("start,end,now,limit,expected", [
    (None, OFFLINE, EVE, DEADLINE, OFFLINE),        # team leaves before it is too late
    (None, DEADLINE, EVE, OFFLINE, OFFLINE),        # too late before the team leaves
    (None, OFFLINE, OFFLINE, DEADLINE, None),       # team already offline
    (MORNING, OFFLINE, EVE, EVE + timedelta(hours=1), None),    # team back only after it is too late
    (MORNING, None, EVE, DEADLINE, DEADLINE),
    (None, None, DEADLINE, EVE, None),              # already too late
])
def test_act_by_respects_the_team_window(start, end, now, limit, expected):
    assert timing.act_by(Window(start, end, "goal"), now, limit) == expected


@pytest.mark.parametrize("now,start,end", [
    ("2030-03-01T08:00:00+01:00", "2030-03-01T09:00:00+01:00", "2030-03-01T17:00:00+01:00"),    # before opening
    ("2030-03-01T11:30:00+01:00", "2030-03-01T11:30:00+01:00", "2030-03-01T17:00:00+01:00"),    # open now
    ("2030-03-01T17:00:00+01:00", "2030-03-04T09:00:00+01:00", "2030-03-04T17:00:00+01:00"),    # closing, weekend
    ("2030-03-02T18:00:00+01:00", "2030-03-04T09:00:00+01:00", "2030-03-04T17:00:00+01:00"),
    ("2030-03-03T23:30:00+00:00", "2030-03-04T09:00:00+01:00", "2030-03-04T17:00:00+01:00"),    # date differs in UTC
    ("2030-03-30T12:00:00+01:00", "2030-04-01T09:00:00+02:00", "2030-04-01T17:00:00+02:00"),    # DST weekend
])
def test_next_office_hours_slot(now, start, end):
    slot = timing.next_office_slot(now, OFFICE)
    assert (slot.start, slot.end) == (at(start), at(end))
    assert slot.source == OFFICE["source"]


def test_office_open_one_day_a_week_finds_next_week():
    weekly = dict(OFFICE, days=["fri"])
    assert timing.next_office_slot("2030-03-01T18:00:00+01:00", weekly).start == at("2030-03-08T09:00:00+01:00")


@pytest.mark.parametrize("change", [{"days": []}, {"days": ["someday"]}, {"timezone": "No/Where"}, {"timezone": "Europe"}, {"to": "08:00"},
                                    {"from": "nine"}, {"source": None}])
def test_bad_office_hours_are_refused(change):
    with pytest.raises(TimingError):
        timing.next_office_slot(EVE, {**OFFICE, **change})


def test_assess_reports_team_and_office_against_too_late_at():
    team = timing.team_window({"team_window": OFFLINE.isoformat()})
    out = assess(team=team, office_hours=OFFICE)
    assert at(out["act_by"]) == at(out["too_late_at"])
    assert at(out["team_window"]["to"]) == OFFLINE
    assert out["office_slot"]["in_time"] is False       # the office opens after it is too late
    assert team.source in out["sources"] and OFFICE["source"] in out["sources"]


# the timeline shape and the counterfactual

@pytest.mark.parametrize("shift", [timedelta(0), timedelta(days=40, hours=5, minutes=17)])
@pytest.mark.parametrize("scale", [1, 3])
def test_hedged_early_lands_and_started_after_cancellation_misses(shift, scale):
    # moved in time and stretched, the shape holds: it follows from the inputs, not from constants
    c = chain(4 * scale, 9 * scale, 1 * scale, 1 * scale)
    early = EVE + shift
    cancelled = early + (MORNING - EVE) * scale
    deadline = early + (DEADLINE - EVE) * scale
    out = assess(c, now=cancelled, needed=deadline, start=early, deadline=deadline, deadline_source="goal")
    assert at(out["planned"]["usable_at"]) == early + timedelta(hours=15 * scale)
    assert out["planned"]["in_time"] and not out["planned"]["misses_deadline"]
    assert at(out["ghost"]["usable_at"]) == cancelled + timedelta(hours=15 * scale)
    assert not out["ghost"]["in_time"] and out["ghost"]["misses_deadline"]


def test_shape_flips_when_the_inputs_change():
    quick = assess(chain(transit=2), now=MORNING, start=EVE)
    assert quick["planned"]["in_time"] and quick["ghost"]["in_time"]
    slow = assess(chain(transit=30), now=MORNING, start=EVE)
    assert not slow["planned"]["in_time"] and not slow["ghost"]["in_time"]


def test_ghost_is_the_same_backup_started_at_scenario_now():
    out = assess(now=MORNING, start=EVE)
    ghost, planned = out["ghost"], out["planned"]
    assert (ghost["kind"], ghost["state"]) == ("ghost", "counterfactual")
    assert at(ghost["start"]) == MORNING
    assert [leg["hours"] for leg in ghost["legs"]] == [leg["hours"] for leg in planned["legs"]]
    assert at(ghost["usable_at"]) - at(planned["usable_at"]) == MORNING - EVE
    assert ghost["late_by_h"] == 1


def test_ghost_follows_the_clock():
    for hours in range(12):
        now = EVE + timedelta(hours=hours)
        assert at(assess(now=now, start=EVE)["ghost"]["usable_at"]) == now + timedelta(hours=15)


def test_needed_before_the_deadline_is_judged_separately():
    needed = datetime(2030, 3, 3, 13, 30, tzinfo=ZONE)
    out = assess(now=EVE + timedelta(hours=6), needed=needed, deadline=DEADLINE, deadline_source="goal")
    assert not out["ghost"]["in_time"] and not out["ghost"]["misses_deadline"]


def test_legs_are_contiguous_and_carry_their_sources():
    bar = timing.timeline(chain(), EVE)
    legs = bar["legs"]
    assert legs[0]["from"] == bar["start"] and legs[-1]["to"] == bar["usable_at"]
    assert all(a["to"] == b["from"] for a, b in zip(legs, legs[1:]))
    assert [leg["source"] for leg in legs] == chain().sources
    assert [leg["label"] for leg in legs] == ["load", "transit", "last mile", "receiving"]


def test_output_is_in_the_zone_of_needed_at():
    out = assess(now=MORNING.astimezone(timezone.utc), start=EVE.astimezone(timezone.utc))
    assert all(v.endswith("+01:00") for v in (out["decide_by"], out["ghost"]["start"], out["planned"]["usable_at"]))


def test_output_fits_the_plan_contracts():
    out = assess(now=MORNING, start=EVE, office_hours=OFFICE)
    json.dumps(out, allow_nan=False)
    contracts.validate("offer", {"offer_id": "o", "supplier_id": "s", "part_id": "p",
                                 "lead_time_h": out["lead_time_h"], "sources": out["sources"]})
    contracts.validate("recommendation", {"kind": "expedite", "part_id": "p", "rationale": "r",
                                          "decide_by": out["decide_by"], "decide_by_why": out["decide_by_why"],
                                          "sources": out["sources"]})
    contracts.validate("promise", {"promise_id": "pr", "supplier_id": "s", "what": "loaded", "state": "waiting",
                                   "due": out["planned"]["legs"][0]["to"]})
    contracts.validate_record({"kind": "plan", "lanes": [{"lane_id": "p", "needed_by": out["needed_at"],
                                                          "bars": [out["planned"], out["ghost"]]}]})


@pytest.mark.parametrize("kwargs", [{"deadline": DEADLINE}, {"valid_until": DEADLINE}, {"needed_source": ""}])
def test_every_time_input_needs_a_source(kwargs):
    with pytest.raises(TimingError):
        timing.assess(chain(), **{"now": EVE, "needed_at": DEADLINE, "needed_source": NEED, **kwargs})


def test_naive_times_are_refused():
    with pytest.raises(TimingError):
        assess(now=EVE.replace(tzinfo=None))
    with pytest.raises(TimingError):
        assess(needed="2030-03-03T20:00:00")
