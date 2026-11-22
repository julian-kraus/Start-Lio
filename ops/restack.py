"""Keep a stacked branch i<n> on top of its parent after the parent changed or was squash-merged.

usage: python ops/restack.py <n>          (run by the author of i<n>; rebases, pushes i<n> only)
       python ops/restack.py <n> --line   (prints the current `Stacked-on:` line for the PR body)
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
ROOT = Path(os.environ.get("RESTACK_ROOT") or Path(__file__).resolve().parent.parent)
LINE = re.compile(r"^Stacked-on: i(\d+)@([0-9a-f]{7,40})\s*$", re.M)
DONE = re.compile(r"^Stacked-on: none\s*$", re.M)   # written once the branch sits on main


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


def merged_parent_shas(parent: int) -> list[str]:
    """Head and commits of the parent's merged PR, newest first: the last parent commits a child can carry."""
    out = run("gh", "pr", "list", "-R", REPO, "--head", f"i{parent}", "--state", "merged",
              "--json", "headRefOid,commits", check=False)
    shas: list[str] = []
    for pr in json.loads(out or "[]"):
        shas.append(pr["headRefOid"])
        shas.extend(c["oid"] for c in reversed(pr.get("commits") or []))
    return shas


def recorded_fork(branch: str, pr: dict | None) -> tuple[int, str] | str | None:
    """(parent, sha) from the PR body, else from the newest marker in the branch; "done" for a `Stacked-on: none`
    marker; None when the branch was never stacked."""
    if pr and (m := LINE.search(pr["body"] or "")):
        return int(m.group(1)), m.group(2)
    for msg in run("git", "log", f"origin/main..origin/{branch}", "--format=%B%x00").split("\x00"):  # newest first
        if DONE.search(msg):
            return "done"
        if (m := LINE.search(msg)):
            return int(m.group(1)), m.group(2)
    return None


def is_ancestor(a: str, b: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=ROOT,
                          capture_output=True).returncode == 0


def resolve_fork(branch: str, parent: int, recorded: str, still_open: bool) -> str | None:
    """The newest parent commit the branch still carries, or None when it carries none (already restacked onto
    main). While the parent exists, the merge base wins when it is newer than the recorded sha (a restack before
    the PR existed, a PR body copied from the claim commit); a force-pushed parent leaves the merge base older,
    then the cut is the recorded sha (old tip) if still held. After a squash merge, the parent PR's last head or commits
    are tried before the recorded sha, so a branch restacked onto a newer parent tip without a marker cuts there."""
    tip = f"origin/{branch}"
    if still_open:
        base = run("git", "merge-base", f"origin/i{parent}", tip)
        return base if is_ancestor(recorded, base) or not is_ancestor(recorded, tip) else recorded
    for sha in [*merged_parent_shas(parent), recorded]:
        if run("git", "cat-file", "-t", sha, check=False) == "commit" and is_ancestor(sha, tip):
            return sha
    return None


def parent_open(parent: int) -> bool:
    return bool(run("git", "ls-remote", "--heads", "origin", f"i{parent}"))


def set_pr(pr: dict, line: str | None) -> bool:
    """Make the PR body carry `line` (or no Stacked-on line) and target main once unstacked. True if it changed."""
    body = pr["body"] or ""
    new = LINE.sub(line, body) if line else LINE.sub("", body).rstrip() + "\n"
    if line and not LINE.search(body):
        new = body.rstrip() + "\n\n" + line + "\n"
    changed = False
    if not line and pr["baseRefName"] != "main":
        run("gh", "pr", "edit", str(pr["number"]), "-R", REPO, "--base", "main")
        changed = True
    if new != body:
        run("gh", "pr", "edit", str(pr["number"]), "-R", REPO, "--body", new)
        changed = True
    return changed


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        raise SystemExit(__doc__)
    n, dry, line_only = int(sys.argv[1]), "--check" in sys.argv, "--line" in sys.argv
    branch = f"i{n}"
    run("git", "fetch", "-q", "origin")
    if not run("git", "ls-remote", "--heads", "origin", branch):
        raise SystemExit(f"origin has no branch {branch}; nothing to restack")
    pr = pr_of(branch)
    rec = recorded_fork(branch, pr)
    if rec is None:
        print(f"{branch} is not stacked; use git rebase origin/main as usual")
        return   # never stacked: leave its PR exactly as it is
    if rec == "done":
        if pr and not dry and not line_only and set_pr(pr, None):
            print(f"{branch}: repaired PR #{pr['number']} (no Stacked-on line, base main)")
        print(f"{branch} is not stacked or no longer stacked; use git rebase origin/main as usual")
        return
    parent, recorded = rec
    still_open = parent_open(parent)
    old = resolve_fork(branch, parent, recorded, still_open)
    if old is None:
        # parent merged and the branch no longer carries any parent commit: restacked already, maybe by a manual
        # push retry that left the PR body behind
        if pr and not dry and not line_only and set_pr(pr, None):
            print(f"{branch}: repaired PR #{pr['number']} (no Stacked-on line, base main)")
        print(f"{branch} is no longer stacked (parent i{parent} merged, no parent commits left)")
        return
    if line_only:
        print(f"Stacked-on: i{parent}@{old}" if still_open else
              f"parent i{parent} merged but {branch} still carries its commits: run python ops/restack.py {n} first")
        return
    base_ref = f"origin/i{parent}" if still_open else "origin/main"
    new_base = run("git", "rev-parse", base_ref)
    if still_open and is_ancestor(new_base, f"origin/{branch}"):
        if pr and not dry and set_pr(pr, f"Stacked-on: i{parent}@{new_base}"):
            print(f"{branch}: PR #{pr['number']} Stacked-on line updated to {new_base[:8]}")
        print(f"{branch} already contains {base_ref} ({new_base[:8]}); nothing to do")
        return
    plan = f"git rebase --onto {base_ref} {old[:12]} {branch}"
    if dry:
        print(f"would run: {plan}; parent i{parent} {'open' if still_open else 'merged'}")
        return
    wt = worktree_of(branch)
    if run("git", "status", "--porcelain", cwd=wt):
        raise SystemExit(f"{wt} has uncommitted changes; commit or stash them first")
    old_head = run("git", "rev-parse", f"origin/{branch}")
    local = f"refs/heads/{branch}"  # the rebase replays this ref: the push would drop what only origin has
    if not is_ancestor(old_head, local) and old_head not in run("git", "rev-list", "-g", local).split():
        raise SystemExit(f"origin/{branch} is at {old_head[:8]}, which the local {branch} never had (a review fix?). "
                         f"Nothing changed. Take it first: git -C {wt} merge --ff-only origin/{branch}, then run again")
    r = subprocess.run(["git", "rebase", "--onto", new_base, old, branch], cwd=wt, capture_output=True, text=True)
    if r.returncode != 0:
        subprocess.run(["git", "rebase", "--abort"], cwd=wt, capture_output=True)
        raise SystemExit(f"conflict while running {plan}; aborted, branch unchanged.\n{r.stdout}{r.stderr}\n"
                         f"Resolve by hand in {wt} with the same command.")
    marker = f"Stacked-on: i{parent}@{new_base}" if still_open else "Stacked-on: none"
    if not pr and marker not in run("git", "log", "-1", "--format=%B", cwd=wt):
        # no PR body to rewrite yet: record the new state in the branch so the next restack reads it
        run("git", "commit", "--allow-empty", "-m", f"restack #{n} onto {base_ref}", "-m", marker, cwd=wt)
    push = subprocess.run(["git", "push", f"--force-with-lease={branch}:{old_head}", "origin", branch], cwd=wt,
                          capture_output=True, text=True)
    if push.returncode != 0:
        raise SystemExit(f"push failed; the local {branch} in {wt} is rewritten but origin still has {old_head[:8]}.\n"
                         f"{push.stderr.strip()}\nRetry: git -C {wt} push --force-with-lease={branch}:{old_head} "
                         f"origin {branch}, then run python ops/restack.py {n} again (it fixes the PR body without "
                         f"rebasing). Or undo locally: git -C {wt} reset --keep {old_head}")
    if pr:
        set_pr(pr, marker if still_open else None)
    print(f"restacked {branch} onto {base_ref} ({new_base[:8]}) and pushed; parent i{parent} "
          f"{'still open' if still_open else 'merged, PR now targets main'}")


if __name__ == "__main__":
    main()
