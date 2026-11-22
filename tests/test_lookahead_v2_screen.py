"""The /lookahead/v2 scripts run in node on synthetic state, with the page stubbed."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "tracker" / "lookahead_static"

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node runs the screen scripts")

PAGE = """
const els = {};
const el = () => ({dataset: {}, style: {}, innerHTML: "", textContent: "", querySelectorAll: () => [],
  classList: {add() {}, remove() {}, toggle() {}}, insertAdjacentHTML() {}});
const ctx = {console, URLSearchParams, location: {hash: ""}, history: {replaceState() {}}, addEventListener() {},
  requestAnimationFrame() {}, setTimeout, clearTimeout, fetch: () => new Promise(() => {}),
  EventSource: class { addEventListener() {} },
  document: {querySelector: s => els[s] = els[s] || el(), querySelectorAll: () => [],
    getElementById: s => els["#" + s] = els["#" + s] || el()}};
ctx.window = ctx;
const vm = require("vm"), fs = require("fs");
vm.createContext(ctx);
for (const f of ["gp_app.js", "gp_plan.js"]) vm.runInContext(fs.readFileSync(process.argv[1] + "/" + f, "utf8"), ctx);
vm.runInContext("S = " + process.argv[2] + "; decisions(); feed(); timeline(); planView(S);", ctx);
console.log(JSON.stringify({lanes: els["#lanes"].innerHTML, feed: els["#feed"].innerHTML, decided: els["#decClosed"].innerHTML, open: els["#decOpen"].innerHTML, plan: els["#v-plan"].innerHTML}));
"""


def render(**state):
    base = {"launch": {"deadline": "2030-01-02T20:00:00+00:00", "name": "Synthetic"}, "goal": None, "plan": None,
            "clock": {"now": "2030-01-01T09:00:00+00:00"}, "events": [], "suppliers": [], "decisions": [],
            "promises": []}
    out = subprocess.run(["node", "-e", PAGE, str(STATIC), json.dumps({**base, **state})],
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def test_decided_table_names_who_decided_not_the_proposed_signer():
    decided = render(decisions=[{"decision_id": "d1", "title": "T", "status": "accepted",
                                 "decided_by": "Deciding Person", "decided_at": "2030-01-01T10:00:00+00:00",
                                 "signer": {"name": "Proposed Person"}}])["decided"]
    assert "Deciding Person" in decided
    assert "Proposed Person" not in decided


def test_coverage_alone_does_not_finish_the_critical_points_step():
    plan = render(plan={"lanes": [], "coverage": {"covered": 1, "needed": 2}})["plan"]
    assert 'class="pf cur" id="pf-1"' in plan
    assert 'class="pf todo" id="pf-3"' in plan
    assert 'class="pf todo" id="pf-4"' in plan


def test_the_feed_lists_only_what_happened_by_the_clock():
    events = [{"id": i, "type": "agent", "title": title, "at": f"2030-01-01T{hour}:00:00+00:00"}
              for i, (title, hour) in enumerate([("Already done", "08"), ("Right now", "09"), ("Still ahead", "10")])]
    feed = render(events=events)["feed"]
    assert "Already done" in feed and "Right now" in feed
    assert "Still ahead" not in feed


def test_a_fact_with_a_date_only_shows_no_time():
    feed = render(events=[{"id": 1, "type": "delivery", "title": "Arrived", "date_only": True,
                           "at": "2030-01-01T08:00:00+00:00"}])["feed"]
    assert "01 Jan" in feed and "09:00" not in feed


def test_the_outside_supplier_is_known_by_its_record_and_names_its_scenario_input():
    suppliers = [{"supplier_id": "outside", "name": "Outside", "score": {"value": 0.5, "n": 4},
                  "scenario": "Borrowed history, scenario input"},
                 {"supplier_id": "known", "name": "Known", "score": {"value": 0.9, "n": 4}}]
    plan = render(suppliers=suppliers, decisions=[{"decision_id": "d1", "title": "T", "status": "open",
                  "supplier_id": "known", "at": "2030-01-01T08:00:00+00:00"}])["plan"]
    assert "Borrowed history, scenario input" in plan
    assert "From order history: Outside" not in plan


def test_every_checkpoint_is_counted():
    promises = [{"promise_id": str(i), "state": "proven", "what": "Proof"} for i in range(4)]
    assert "Checkpoints 4 / 4" in render(promises=promises)["plan"]


def test_chips_at_the_same_time_each_get_a_row():
    events = [{"id": i, "type": "document", "supplier_id": "s", "title": f"Synthetic finding number {i}",
               "at": "2030-01-01T09:00:00+00:00"} for i in range(4)]
    tops = re.findall(r'class="ev [^"]*" style="[^"]*top:([^;]+);', render(events=events)["lanes"])
    assert len(tops) == 4 and len(set(tops)) == 4


def test_the_decision_card_names_the_sources_of_its_amounts_and_escapes_them():
    card = render(decisions=[{"decision_id": "d1", "title": "T", "status": "open", "at": "2030-01-01T08:00:00+00:00",
                              "cost": {"eur": 5, "sources": [{"fact_id": "fact-cost"}]},
                              "impact": {"eur": 9, "units": 3, "sources": [{"scenario_input": "<goal answer>"}]}}])["open"]
    assert "Source: kb facts fact-cost" in card
    assert "scenario input: &lt;goal answer&gt;" in card
    assert "<goal answer>" not in card


def test_a_number_without_a_source_says_so():
    card = render(decisions=[{"decision_id": "d1", "title": "T", "status": "open", "at": "2030-01-01T08:00:00+00:00",
                              "cost": {"eur": 5}, "impact": {"eur": 9}}])["open"]
    assert "Source not on the record" in card
