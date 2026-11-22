"""The /lookahead/v2 scripts run in node on synthetic state, with the page stubbed."""

import json
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
vm.runInContext("S = " + process.argv[2] + "; decisions(); planView(S);", ctx);
console.log(JSON.stringify({decided: els["#decClosed"].innerHTML, plan: els["#v-plan"].innerHTML}));
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
