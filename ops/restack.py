"""Keep a stacked branch i<n> on top of its parent after the parent changed or was squash-merged.

usage: python ops/restack.py <n>          (run by the author of i<n>; rebases, pushes i<n> only)
       python ops/restack.py <n> --check  (prints what it would do, changes nothing)

A stacked branch records its fork point in a line `Stacked-on: i<m>@<sha>`: in the PR body once the PR exists,
before that in the claim commit message. After the parent is squash-merged, GitHub deletes i<m> and retargets the
PR to main, but i<n> still carries the parent's old commits. This script replays only the commits after the fork
point onto the new base (origin/main if the parent merged, origin/i<m> if it is still open), pushes i<n> with
--force-with-lease, and rewrites the Stacked-on line (or removes it once the base is main).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = os.environ.get("LOOKAHEAD_REPO", "julian-kraus/Start-Lio")
ROOT = Path(__file__).resolve().parent.parent
LINE = re.compile(r"^Stacked-on: i(\d+)@([0-9a-f]{7,40})\s*$", re.M)


def run(*args: str, cwd: Path = ROOT, check: bool = True) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"{' '.join(args)} failed:\n{r.stdout}{r.stderr}")
    return r.stdout.strip()


def worktree_of(branch: str) -> Path:
    path = None
    for line in run("git", "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = Path(line.split(" ", 1)[1])
        elif line == f"branch refs/heads/{branch}" and path:
            return path
    raise SystemExit(f"{branch} is not checked out in any worktree; create .worktrees/{branch[1:]} first")


def pr_of(branch: str) -> dict | None:
    out = run("gh", "pr", "list", "-R", REPO, "--head", branch, "--state", "open",
              "--json", "number,body,baseRefName")
    prs = json.loads(out or "[]")
    return prs[0] if prs else None


def fork_point(branch: str, pr: dict | None) -> tuple[int, str] | None:
    if pr and (m := LINE.search(pr["body"] or "")):
        return int(m.group(1)), m.group(2)
    if not run("git", "ls-remote", "--heads", "origin", branch):
        raise SystemExit(f"origin has no branch {branch}; nothing to restack")
    msgs = run("git", "log", f"origin/main..origin/{branch}", "--format=%B%x00")
    for msg in msgs.split("\x00"):
        if (m := LINE.search(msg)):
            return int(m.group(1)), m.group(2)
    return None


def parent_open(parent: int) -> bool:
    return bool(run("git", "ls-remote", "--heads", "origin", f"i{parent}"))


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        raise SystemExit(__doc__)
    n, dry = int(sys.argv[1]), "--check" in sys.argv
    branch = f"i{n}"
    run("git", "fetch", "-q", "origin")
    pr = pr_of(branch)
    fp = fork_point(branch, pr)
    if fp is None:
        print(f"{branch} is not stacked (no Stacked-on line); use git rebase origin/main as usual")
        return
    parent, old = fp
    still_open = parent_open(parent)
    base_ref = f"origin/i{parent}" if still_open else "origin/main"
    new_base = run("git", "rev-parse", base_ref)
    contains = subprocess.run(["git", "merge-base", "--is-ancestor", new_base, f"origin/{branch}"],
                              cwd=ROOT).returncode == 0
    if still_open and contains:
        print(f"{branch} already contains {base_ref} ({new_base[:8]}); nothing to do")
        return
    plan = f"git rebase --onto {base_ref} {old[:12]} {branch}"
    if dry:
        print(f"would run: {plan}; parent i{parent} {'open' if still_open else 'merged'}")
        return
    wt = worktree_of(branch)
    if run("git", "status", "--porcelain", cwd=wt):
        raise SystemExit(f"{wt} has uncommitted changes; commit or stash them first")
    r = subprocess.run(["git", "rebase", "--onto", new_base, old, branch], cwd=wt, capture_output=True, text=True)
    if r.returncode != 0:
        subprocess.run(["git", "rebase", "--abort"], cwd=wt)
        raise SystemExit(f"conflict while running {plan}; aborted, branch unchanged.\n{r.stdout}{r.stderr}\n"
                         f"Resolve by hand in {wt} with the same command.")
    run("git", "push", "--force-with-lease", "origin", branch, cwd=wt)
    if pr:
        body = pr["body"] or ""
        if still_open:
            body = LINE.sub(f"Stacked-on: i{parent}@{new_base}", body)
        else:
            body = LINE.sub("", body).rstrip() + "\n"
            if pr["baseRefName"] != "main":
                run("gh", "pr", "edit", str(pr["number"]), "-R", REPO, "--base", "main")
        run("gh", "pr", "edit", str(pr["number"]), "-R", REPO, "--body", body)
    print(f"restacked {branch} onto {base_ref} ({new_base[:8]}) and pushed; parent i{parent} "
          f"{'still open' if still_open else 'merged, PR now targets main'}")


if __name__ == "__main__":
    main()
