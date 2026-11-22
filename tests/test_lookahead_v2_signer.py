"""/lookahead/v2 shows and posts only the signer the card route answers (#212). The scripts run in node."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "tracker" / "lookahead_static"

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node runs the screen scripts")

# argv: static dir, JSON list of card answers in order: a card, "fail" or "hold" (never answers)
HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const answers = JSON.parse(process.argv[2]), els = {}, posts = [], alerts = [];
const el = () => ({dataset: {}, style: {}, innerHTML: "", textContent: "", querySelectorAll: () => [],
  classList: {add() {}, remove() {}, toggle() {}}, insertAdjacentHTML() {}});
const state = {launch: {deadline: "2030-01-02T20:00:00+00:00", name: "Synthetic"}, goal: null, plan: null,
  clock: {now: "2030-01-01T09:00:00+00:00"}, events: [], suppliers: [], promises: [],
  decisions: [{decision_id: "replan:P-1:x", status: "open", title: "T", at: "2030-01-01T09:00:00+00:00",
               signer: {name: "Producer Person", role: "Proposer"}}]};
const ok = body => Promise.resolve({ok: true, json: () => Promise.resolve(body)});
const ctx = {console, URLSearchParams, location: {hash: ""}, history: {replaceState() {}}, addEventListener() {},
  requestAnimationFrame() {}, setTimeout, clearTimeout, alert: m => alerts.push(m),
  prompt: () => "Typed Person", EventSource: class { addEventListener() {} },
  fetch(path, init) {
    if (path === "/api/lookahead/state") return ok(state);
    if (init && init.method === "POST") { posts.push({path, body: JSON.parse(init.body)}); return ok({ok: true}); }
    const a = answers.shift();
    if (a === "hold") return new Promise(() => {});
    return a === undefined || a === "fail" ? Promise.resolve({ok: false, json: () => Promise.resolve({})}) : ok(a);
  },
  document: {querySelector: s => els[s] = els[s] || el(), querySelectorAll: () => [],
    getElementById: s => els["#" + s] = els["#" + s] || el()}};
ctx.window = ctx;
vm.createContext(ctx);
for (const f of ["gp_app.js", "gp_plan.js"]) vm.runInContext(fs.readFileSync(process.argv[1] + "/" + f, "utf8"), ctx);
const tick = () => new Promise(r => setTimeout(r, 10));
(async () => {
  await tick();
  const shown = {queue: els["#dq"].innerHTML, card: els["#decOpen"].innerHTML, plan: els["#v-plan"].innerHTML};
  vm.runInContext('accept("replan:P-1:x")', ctx);
  await tick();
  console.log(JSON.stringify({shown, posts, alerts}));
  process.exit(0);
})();
"""

ROUTED = {"signer": {"name": "Routed Person", "role": "Approver"}, "approval": {"status": "resolved"}}
OTHER = {"signer": {"name": "Other Person", "role": "Approver"}, "approval": {"status": "resolved"}}
NOBODY = {"signer": {"role": "Approver", "reason": "no holder"}, "approval": {"status": "needs_human"}}
UNRESOLVED = {"signer": {"name": "Script Person", "role": "Buyer", "status": "needs_human"}}


def run(*answers):
    out = subprocess.run(["node", "-e", HARNESS, str(STATIC), json.dumps(answers)], capture_output=True, text=True,
                         timeout=30, check=True)
    return json.loads(out.stdout)


def test_every_place_shows_the_routed_signer_and_accept_posts_that_name():
    r = run(ROUTED, ROUTED, ROUTED)
    for html in r["shown"].values():
        assert "Routed Person" in html and "Producer Person" not in html
    assert r["posts"] == [{"path": "/api/lookahead/decisions/replan%3AP-1%3Ax/accept",
                           "body": {"approver": "Routed Person"}}]


def test_without_a_signer_the_screen_says_needs_human_and_posts_nothing():
    r = run(NOBODY, NOBODY)
    for html in r["shown"].values():
        assert "needs_human" in html and "Producer Person" not in html
    assert "disabled" in r["shown"]["card"]
    assert r["posts"] == []


def test_an_unresolved_script_signer_is_not_presented_as_resolved():
    r = run(UNRESOLVED, UNRESOLVED)
    for html in r["shown"].values():
        assert "needs_human" in html
        assert "Accept as Script Person" not in html, "an unresolved signer is never offered as the one who accepts"
    assert "disabled" in r["shown"]["card"] and "disabled" in r["shown"]["queue"]
    assert r["posts"] == []


def test_a_failed_or_changed_lookup_never_opens_a_browser_dialog():
    for answers in (["hold"], ["fail"], [ROUTED, "fail"], [ROUTED, OTHER]):
        assert run(*answers)["alerts"] == [], "a dialog blocks the page; the screen says it inline"


@pytest.mark.parametrize("answers", [["hold"], ["fail"], [ROUTED, "fail"], [ROUTED, OTHER]])
def test_a_missing_failed_or_changed_lookup_posts_nothing(answers):
    assert run(*answers)["posts"] == []
