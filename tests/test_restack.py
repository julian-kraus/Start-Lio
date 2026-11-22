"""ops/restack.py against a throwaway local origin, with the GitHub calls stubbed."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "ops" / "restack.py"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(cwd: Path, name: str, msg: str | None = None) -> str:
    (cwd / name).write_text(name + "\n")
    git(cwd, "add", name)
    git(cwd, "commit", "-qm", msg or name)
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(work))
    for k, v in (("user.email", "t@example.invalid"), ("user.name", "t")):
        git(work, "config", k, v)
    commit(work, "base")
    git(work, "push", "-q", "origin", "main")
    monkeypatch.setenv("RESTACK_ROOT", str(work))
    return work


class Stub:
    """Loads restack.py fresh and records the gh calls it makes."""

    def __init__(self, monkeypatch, pr=None, merged=()):
        spec = importlib.util.spec_from_file_location("restack_under_test", SCRIPT)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.gh_calls: list[tuple[str, ...]] = []
        real_run = self.mod.run

        def run(*args, **kw):
            if args[0] == "gh":
                self.gh_calls.append(args)
                return ""
            return real_run(*args, **kw)

        monkeypatch.setattr(self.mod, "run", run)
        monkeypatch.setattr(self.mod, "pr_of", lambda branch: pr)
        monkeypatch.setattr(self.mod, "merged_parent_shas", lambda parent: list(merged))

    def __call__(self, monkeypatch, *argv: str) -> None:
        monkeypatch.setattr(sys, "argv", ["restack.py", *argv])
        self.mod.main()


def stack_child(work: Path) -> tuple[str, str]:
    """i1 with one commit; i2 claimed on it with a Stacked-on line and one commit of its own."""
    git(work, "switch", "-q", "-c", "i1")
    p1 = commit(work, "p1")
    git(work, "push", "-q", "origin", "i1")
    git(work, "switch", "-q", "-c", "i2")
    git(work, "commit", "-q", "--allow-empty", "-m", "claim #2 agent", "-m", f"Stacked-on: i1@{p1}")
    commit(work, "c1")
    git(work, "push", "-q", "origin", "i2")
    return p1, git(work, "rev-parse", "HEAD")


def squash_merge_parent(work: Path) -> None:
    git(work, "switch", "-q", "main")
    git(work, "merge", "-q", "--squash", "origin/i1")
    git(work, "commit", "-qm", "parent squashed")
    git(work, "push", "-q", "origin", "main")
    git(work, "push", "-q", "origin", "--delete", "i1")
    git(work, "switch", "-q", "i2")


def child_files(work: Path) -> list[str]:
    git(work, "fetch", "-q", "origin")
    return git(work, "diff", "--name-only", "origin/main...origin/i2").split()


def test_squash_merged_parent_replays_only_the_child_then_stops(repo, monkeypatch, capsys):
    stack_child(repo)
    squash_merge_parent(repo)
    Stub(monkeypatch)(monkeypatch, "2")
    assert child_files(repo) == ["c1"]
    head = git(repo, "rev-parse", "origin/i2")
    git(repo, "switch", "-q", "main"); commit(repo, "later"); git(repo, "push", "-q", "origin", "main")
    git(repo, "switch", "-q", "i2")
    Stub(monkeypatch)(monkeypatch, "2")
    assert "no longer stacked" in capsys.readouterr().out
    assert git(repo, "rev-parse", "origin/i2") == head, "a formerly stacked branch is left alone when main moves"


def test_restack_before_the_pr_records_the_fork_point(repo, monkeypatch):
    stack_child(repo)
    git(repo, "switch", "-q", "i1"); commit(repo, "p2"); git(repo, "push", "-q", "origin", "i1")
    git(repo, "switch", "-q", "i2")
    Stub(monkeypatch)(monkeypatch, "2")                      # parent open: onto the new i1 tip, marker written
    assert "Stacked-on: i1@" + git(repo, "rev-parse", "origin/i1") in git(repo, "log", "-1", "--format=%B")
    squash_merge_parent(repo)
    Stub(monkeypatch)(monkeypatch, "2")                      # parent merged: only the child's file is left
    assert child_files(repo) == ["c1"]


def test_unmarked_restack_uses_the_parents_last_head_after_the_merge(repo, monkeypatch):
    p1, _ = stack_child(repo)
    git(repo, "switch", "-q", "i1"); p2 = commit(repo, "p2"); git(repo, "push", "-q", "origin", "i1")
    git(repo, "switch", "-q", "i2")
    git(repo, "rebase", "-q", "--onto", p2, p1, "i2")      # an old-style restack: no marker written
    git(repo, "push", "-q", "--force", "origin", "i2")
    squash_merge_parent(repo)
    Stub(monkeypatch, merged=[p2, p2, p1])(monkeypatch, "2")
    assert child_files(repo) == ["c1"]


def test_pr_on_main_without_a_line_still_restacks(repo, monkeypatch):
    stack_child(repo)
    squash_merge_parent(repo)                                # GitHub retargeted the PR; its body never had the line
    pr = {"number": 7, "body": "Closes #2", "baseRefName": "main"}
    Stub(monkeypatch, pr=pr)(monkeypatch, "2")
    assert child_files(repo) == ["c1"]


def test_rerun_after_a_manual_push_repairs_the_pr_without_rebasing(repo, monkeypatch, capsys):
    p1, _ = stack_child(repo)
    squash_merge_parent(repo)
    git(repo, "rebase", "-q", "--onto", "origin/main", p1, "i2")   # the printed retry: pushed, body not fixed
    git(repo, "push", "-q", "--force", "origin", "i2")
    head = git(repo, "rev-parse", "origin/i2")
    pr = {"number": 7, "body": f"Closes #2\n\nStacked-on: i1@{p1}\n", "baseRefName": "main"}
    stub = Stub(monkeypatch, pr=pr, merged=[p1])
    stub(monkeypatch, "2")
    assert git(repo, "rev-parse", "origin/i2") == head
    edits = [c for c in stub.gh_calls if "--body" in c]
    assert edits and "Stacked-on" not in edits[0][-1]
    assert "repaired PR #7" in capsys.readouterr().out


def test_line_reports_the_merge_base_not_the_stale_claim(repo, monkeypatch, capsys):
    stack_child(repo)
    git(repo, "switch", "-q", "i1"); commit(repo, "p2"); git(repo, "push", "-q", "origin", "i1")
    git(repo, "switch", "-q", "i2")
    git(repo, "rebase", "-q", "origin/i1"); git(repo, "push", "-q", "--force", "origin", "i2")
    Stub(monkeypatch)(monkeypatch, "2", "--line")
    assert capsys.readouterr().out.strip() == "Stacked-on: i1@" + git(repo, "rev-parse", "origin/i1")


def test_a_branch_never_stacked_is_left_alone(repo, monkeypatch, capsys):
    git(repo, "switch", "-q", "-c", "i2"); commit(repo, "c1"); git(repo, "push", "-q", "origin", "i2")
    pr = {"number": 7, "body": "Closes #2", "baseRefName": "feature"}
    stub = Stub(monkeypatch, pr=pr)
    stub(monkeypatch, "2")
    assert stub.gh_calls == [], "a PR that was never stacked is neither retargeted nor edited"
    assert "not stacked" in capsys.readouterr().out


def test_line_prints_a_fork_point_the_branch_holds(repo, monkeypatch, capsys):
    p1, _ = stack_child(repo)
    git(repo, "switch", "-q", "i1"); git(repo, "commit", "-q", "--amend", "-m", "p1 reworded")
    git(repo, "push", "-q", "--force", "origin", "i1")          # parent force-pushed: recorded p1 still in i2
    git(repo, "switch", "-q", "i2")
    Stub(monkeypatch)(monkeypatch, "2", "--line")
    sha = capsys.readouterr().out.strip().split("@")[1]
    assert git(repo, "merge-base", "--is-ancestor", sha, "origin/i2") == ""


def test_next_restack_after_a_hand_restack_cuts_at_the_merge_base(repo, monkeypatch):
    p1, _ = stack_child(repo)
    git(repo, "switch", "-q", "i1")
    (repo / "p1").write_text("amended\n"); git(repo, "commit", "-qa", "--amend", "-m", "p1 amended")
    git(repo, "push", "-q", "--force", "origin", "i1")
    git(repo, "rebase", "-q", "--onto", "i1", p1, "i2"); git(repo, "push", "-q", "--force", "origin", "i2")
    git(repo, "switch", "-q", "i1")
    (repo / "p1").write_text("third\n"); git(repo, "commit", "-qam", "p3"); git(repo, "push", "-q", "origin", "i1")
    git(repo, "switch", "-q", "i2")
    Stub(monkeypatch)(monkeypatch, "2")
    assert git(repo, "diff", "--name-only", "origin/i1...origin/i2").split() == ["c1"]
