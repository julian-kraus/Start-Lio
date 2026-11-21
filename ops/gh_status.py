"""Keep issue labels and the "Lookahead night" project in step.

usage:
  python ops/gh_status.py claim   <issue> <agent-id>
  python ops/gh_status.py review  <issue>             (PR opened)
  python ops/gh_status.py pass    <pr> [<agent-id>]   (reviewer: every acceptance item verified)
  python ops/gh_status.py changes <pr> "<reason>"     (reviewer: items unmet)
  python ops/gh_status.py block   <issue> "<reason>"
  python ops/gh_status.py release <issue> "<reason>"  (coordinator: stale claim back to ready)
  python ops/gh_status.py ready   <issue>             (coordinator: last blocker closed)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

REPO = os.environ.get("LOOKAHEAD_REPO", "julian-kraus/Start-Lio")
OWNER = REPO.split("/")[0]
PROJECT_TITLE = os.environ.get("LOOKAHEAD_PROJECT", "Lookahead night")
STATUS_LABELS = ["status:ready", "status:waiting", "status:claimed", "status:in-review",
                 "status:changes-requested", "status:blocked"]
PROJECT_STATUS = {"status:ready": "Ready", "status:waiting": "Waiting", "status:claimed": "Claimed",
                  "status:in-review": "In review", "status:changes-requested": "Changes requested",
                  "status:blocked": "Blocked"}


def gh(*args: str) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"gh {' '.join(args[:3])}: {r.stderr.strip()[:400]}")
    return r.stdout


def set_status(issue: int, label: str, agent: str | None = None) -> None:
    remove = [x for x in STATUS_LABELS if x != label]
    gh("issue", "edit", str(issue), "-R", REPO, "--add-label", label, "--remove-label", ",".join(remove))
    project_fields(issue, PROJECT_STATUS[label], agent)


def project_fields(issue: int, status: str, agent: str | None) -> None:
    projects = json.loads(gh("project", "list", "--owner", OWNER, "--format", "json"))["projects"]
    proj = next((p for p in projects if p["title"] == PROJECT_TITLE), None)
    if proj is None:
        return
    num, pid = str(proj["number"]), proj["id"]
    fields = {f["name"]: f for f in json.loads(gh("project", "field-list", num, "--owner", OWNER,
                                                   "--format", "json", "--limit", "50"))["fields"]}
    items = json.loads(gh("project", "item-list", num, "--owner", OWNER, "--format", "json", "--limit", "200"))
    item = next((i for i in items["items"] if (i.get("content") or {}).get("number") == issue), None)
    if item is None:
        return
    opt = next(o["id"] for o in fields["Status"]["options"] if o["name"] == status)
    gh("project", "item-edit", "--id", item["id"], "--project-id", pid, "--field-id", fields["Status"]["id"],
       "--single-select-option-id", opt)
    if agent is not None and "Agent" in fields:
        if agent:
            gh("project", "item-edit", "--id", item["id"], "--project-id", pid, "--field-id",
               fields["Agent"]["id"], "--text", agent)
        else:
            gh("project", "item-edit", "--id", item["id"], "--project-id", pid, "--field-id",
               fields["Agent"]["id"], "--clear")


def issue_of_pr(pr: int) -> int:
    body = json.loads(gh("pr", "view", str(pr), "-R", REPO, "--json", "body"))["body"] or ""
    m = re.search(r"(?i)closes\s+#(\d+)", body)
    if not m:
        raise SystemExit(f"PR #{pr} body has no 'Closes #<n>'")
    return int(m.group(1))


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        raise SystemExit(__doc__)
    cmd, num = argv[0], int(argv[1])
    extra = argv[2] if len(argv) > 2 else ""
    if cmd == "claim":
        if not extra:
            raise SystemExit("claim needs an agent id")
        gh("issue", "edit", str(num), "-R", REPO, "--add-assignee", "@me")
        set_status(num, "status:claimed", extra)
        gh("issue", "comment", str(num), "-R", REPO, "--body", f"claim {extra} branch i{num}")
    elif cmd == "review":
        set_status(num, "status:in-review")
    elif cmd == "pass":
        issue = issue_of_pr(num)
        gh("pr", "edit", str(num), "-R", REPO, "--add-label", "review:pass", "--remove-label", "review:changes")
        gh("pr", "comment", str(num), "-R", REPO, "--body",
           f"review:pass{' by ' + extra if extra else ''}: every acceptance item verified on the PR head")
        project_fields(issue, "In review", None)
    elif cmd == "changes":
        issue = issue_of_pr(num)
        gh("pr", "edit", str(num), "-R", REPO, "--add-label", "review:changes", "--remove-label", "review:pass")
        gh("pr", "comment", str(num), "-R", REPO, "--body", f"review:changes: {extra}")
        set_status(issue, "status:changes-requested")
    elif cmd == "block":
        set_status(num, "status:blocked")
        gh("issue", "comment", str(num), "-R", REPO, "--body", f"blocked: {extra}")
    elif cmd == "release":
        gh("issue", "edit", str(num), "-R", REPO, "--remove-assignee", "@me")
        set_status(num, "status:ready", "")
        gh("issue", "comment", str(num), "-R", REPO, "--body",
           f"claim released by the coordinator: {extra}. The next agent continues on branch i{num}.")
    elif cmd == "ready":
        set_status(num, "status:ready")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
