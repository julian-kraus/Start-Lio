"""The decision card posts the routed signer, not the one stored on the decision (#201)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CARD = ROOT / "tracker" / "lookahead_static" / "decision_card.js"

HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const calls = [];
const ctx = {
  S: {}, console,
  el: (tag, attrs, ...kids) => ({ tag, ...(attrs || {}), textContent: kids.flat().filter((k) => typeof k === "string").join("") }),
  api: async (path, body) => {
    calls.push({ path, body: body === undefined ? null : body });
    return body === undefined ? { signer: { name: "Jo James", role: "Area Manager" } } : { ok: true, sent: [] };
  },
  toast: () => {}, render: () => {},
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
(async () => {
  const err = { textContent: "" };
  const b = ctx.acceptButton({ decision_id: "replan:P-1:x", signer: { name: "Mallory Producer" } }, err);
  await new Promise((r) => setTimeout(r, 0));
  await b.onclick();
  console.log(JSON.stringify({ calls, label: b.textContent, error: err.textContent }));
})();
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_accept_posts_the_routed_signer_not_the_stored_one(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS)
    out = json.loads(subprocess.run(["node", str(harness), str(CARD)], capture_output=True, text=True,
                                    check=True).stdout)
    posts = [c for c in out["calls"] if c["body"] is not None]
    assert posts == [{"path": "/api/lookahead/decisions/replan%3AP-1%3Ax/accept", "body": {"approver": "Jo James"}}]
    assert any(c["path"] == "/api/lookahead/decisions/replan%3AP-1%3Ax" and c["body"] is None for c in out["calls"])
    assert out["label"] == "Accept as Jo James" and out["error"] == ""

