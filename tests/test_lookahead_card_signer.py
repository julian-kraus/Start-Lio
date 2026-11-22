"""The decision card accepts only in the name it shows, and that name is the routed signer (#201). The card runs
in node with stubbed `el` and `api`; each case scripts what the card route answers."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CARD = Path(__file__).resolve().parent.parent / "tracker" / "lookahead_static" / "decision_card.js"

# argv: card path, JSON list of card-route answers in order: {"name": ...}, "fail" or "hold" (never answers)
HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const answers = JSON.parse(process.argv[3]), calls = [];
const ctx = {
  S: {}, console, toast: () => {}, render: () => {}, initials: (n) => n.split(" ").map((w) => w[0]).join(""),
  el: (tag, attrs, ...kids) => ({ tag, ...(attrs || {}), textContent: kids.flat().filter((k) => typeof k === "string").join("") }),
  api: (path, body) => {
    calls.push({ path, body: body === undefined ? null : body });
    if (body !== undefined) return Promise.resolve({ ok: true, sent: [] });
    const a = answers.shift();
    if (a === "hold") return new Promise(() => {});
    if (a === "fail") return Promise.reject(new Error("503"));
    return Promise.resolve({ signer: { name: a.name, role: "Area Manager" } });
  },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
(async () => {
  const err = { textContent: "" };
  const b = ctx.acceptButton({ decision_id: "replan:P-1:x", signer: { name: "Mallory Producer" } }, err);
  const tick = () => new Promise((r) => setTimeout(r, 0));
  await tick();
  const before = { label: b.textContent, disabled: !!b.disabled };
  await b.onclick();
  await tick();
  console.log(JSON.stringify({ before, calls, label: b.textContent, disabled: !!b.disabled, error: err.textContent }));
})();
"""


def run(tmp_path, *answers):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS)
    out = subprocess.run(["node", str(harness), str(CARD), json.dumps(list(answers))], capture_output=True,
                         text=True, check=True, timeout=20)
    result = json.loads(out.stdout)
    result["posts"] = [c for c in result["calls"] if c["body"] is not None]
    return result


pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
JO, ANN = {"name": "Jo James"}, {"name": "Ann Other"}


def test_accept_posts_exactly_the_shown_routed_signer(tmp_path):
    r = run(tmp_path, JO, JO)
    assert r["before"] == {"label": "Accept as Jo James", "disabled": False}
    assert r["posts"] == [{"path": "/api/lookahead/decisions/replan%3AP-1%3Ax/accept", "body": {"approver": "Jo James"}}]


def test_a_click_before_the_signer_is_loaded_posts_nothing(tmp_path):
    r = run(tmp_path, "hold")
    assert r["before"]["disabled"] and "Accept as" not in r["before"]["label"]
    assert r["posts"] == []


def test_a_changed_signer_posts_nothing_and_updates_the_label(tmp_path):
    r = run(tmp_path, JO, ANN)
    assert r["before"]["label"] == "Accept as Jo James"
    assert r["posts"] == [] and r["label"] == "Accept as Ann Other" and "changed" in r["error"]


def test_a_failed_lookup_on_click_posts_nothing(tmp_path):
    r = run(tmp_path, JO, "fail")
    assert r["posts"] == [] and r["label"] == "Accept as Jo James" and "Nothing was sent" in r["error"]
