#!/usr/bin/env python3
"""The one command every agent runs before a PR: python ops/check.py

It starts (or reuses) a local throwaway Postgres in Docker, points TEST_DATABASE_URL at it, and runs the
checks that gate a PR:
  - line-length guard: no code file over 400 lines except the grandfather list (which may not grow)
  - byte-compile of the source packages
  - pytest -q
  - the kb smoke (migrate, seed-rules, load, reconcile) on the throwaway database

It prints one summary line, CHECK PASS <sha> or CHECK FAIL <sha> <what failed>, and exits non-zero on failure.

It never touches a live database: conftest refuses any URL containing "supabase" or "pooler", and this runner
only ever connects to the container it starts. All knobs come from the environment (see ENV below) so the port
and container name can be moved without editing code.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Code files longer than this fail the guard, unless they are grandfathered below.
LINE_LIMIT = 400
CODE_SUFFIXES = {".py", ".sql", ".html", ".js", ".ts", ".css", ".yaml"}
# Directories the guard and the compile step never descend into (data, generated, vendored, tests).
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".worktrees", "data", "mail_data", "exports",
             "secrets", ".pytest_cache", "tests", "analysis", "challenge", ".claude"}
# Files already over the limit. The value is their current size: they may shrink but never grow.
# tracker/lookahead_page.html and tracker/web.py arrive over the limit with the Lookahead screen and are
# split by issue U1 (AGENTS.md); nobody else grows them.
GRANDFATHER = {
    "structured/build_structured.py": 1063,
    "tracker/web_page.html": 499,
    "tracker/lookahead_page.html": 1239,
    "tracker/web.py": 500,
}

# ENV knobs (all optional):
CONTAINER = os.environ.get("CHECK_DB_CONTAINER", "lookahead-check-db")
DB_PORT = os.environ.get("CHECK_DB_PORT", "5440")
DB_IMAGE = os.environ.get("CHECK_DB_IMAGE", "postgres:17-alpine")
DB_USER = os.environ.get("CHECK_DB_USER", "check")
DB_PASSWORD = os.environ.get("CHECK_DB_PASSWORD", "check")
DB_NAME = os.environ.get("CHECK_DB_NAME", "check")
TIME_BUDGET = float(os.environ.get("CHECK_TIME_BUDGET", "360"))  # seconds; reported, not enforced
READY_TIMEOUT = float(os.environ.get("CHECK_DB_READY_TIMEOUT", "60"))
KB_SMOKE = ("migrate", "seed-rules", "load", "reconcile")


def run(cmd: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a command, stream a captured copy of its output, and return the completed process."""
    proc = subprocess.run(cmd, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.stdout:
        sys.stdout.write(proc.stdout)
        sys.stdout.flush()
    return proc


def docker_state() -> str:
    """"running", "stopped", or "absent" for the check container."""
    p = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
                       text=True, capture_output=True)
    if p.returncode != 0:
        return "absent"
    return "running" if p.stdout.strip() == "true" else "stopped"


def ensure_db() -> tuple[bool, str]:
    """Start or reuse the throwaway Postgres. Returns (ok, message)."""
    if subprocess.run(["docker", "version"], capture_output=True).returncode != 0:
        return False, "docker is not available"
    state = docker_state()
    if state == "running":
        msg = f"reusing container {CONTAINER}"
    elif state == "stopped":
        if subprocess.run(["docker", "start", CONTAINER], capture_output=True).returncode != 0:
            return False, f"could not start existing container {CONTAINER}"
        msg = f"started existing container {CONTAINER}"
    else:
        created = subprocess.run(
            ["docker", "run", "-d", "--name", CONTAINER,
             "-e", f"POSTGRES_USER={DB_USER}", "-e", f"POSTGRES_PASSWORD={DB_PASSWORD}",
             "-e", f"POSTGRES_DB={DB_NAME}", "-p", f"127.0.0.1:{DB_PORT}:5432", DB_IMAGE],
            capture_output=True, text=True)
        if created.returncode != 0:
            return False, f"could not start container: {created.stderr.strip()[:200]}"
        msg = f"started new container {CONTAINER} on port {DB_PORT}"
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        # over TCP: while a new container initialises, a temporary server answers on the socket only
        ready = subprocess.run(["docker", "exec", CONTAINER, "pg_isready", "-h", "127.0.0.1",
                                "-U", DB_USER, "-d", DB_NAME], capture_output=True)
        if ready.returncode == 0:
            return True, msg
        time.sleep(1)
    return False, f"{msg}, but it was not ready within {int(READY_TIMEOUT)}s"


def line_guard() -> list[str]:
    """Return a list of offending "path: N lines (limit M)" strings; empty means the guard passed."""
    offenders = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in CODE_SUFFIXES:
            continue
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        n = len(path.read_text(errors="replace").splitlines())
        cap = GRANDFATHER.get(rel.as_posix(), LINE_LIMIT)
        if n > cap:
            note = " grandfather cap" if rel.as_posix() in GRANDFATHER else ""
            offenders.append(f"{rel.as_posix()}: {n} lines (limit {cap}{note})")
    return offenders


def package_dirs() -> list[str]:
    """Top-level source packages to byte-compile (anything with .py files, minus the skip dirs and tests)."""
    dirs = []
    for child in sorted(ROOT.iterdir()):
        if not child.is_dir() or child.name in SKIP_DIRS or child.name.startswith("."):
            continue
        if any(child.rglob("*.py")):
            dirs.append(child.name)
    return dirs


def pytest_failures(output: str) -> list[str]:
    """Pull the FAILED/ERROR node ids out of a pytest run for the summary line."""
    nodes = re.findall(r"^(?:FAILED|ERROR)\s+(\S+)", output, re.MULTILINE)
    return nodes


def main() -> int:
    start = time.monotonic()
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True,
                         capture_output=True).stdout.strip() or "unknown"
    failing: list[str] = []

    print("== line-length guard ==")
    offenders = line_guard()
    if offenders:
        print("files over the line limit:")
        for o in offenders:
            print(f"  {o}")
        failing.append("line-length")
    else:
        print(f"ok: no code file over {LINE_LIMIT} lines (grandfathered: {', '.join(sorted(GRANDFATHER))})")

    print("\n== byte-compile ==")
    pkgs = package_dirs()
    compiled = run([sys.executable, "-m", "compileall", "-q", *pkgs])
    if compiled.returncode != 0:
        failing.append("compile")
    else:
        print(f"ok: compiled {', '.join(pkgs)}")

    print("\n== throwaway Postgres ==")
    db_ok, db_msg = ensure_db()
    print(db_msg)
    url = f"postgresql://{DB_USER}:{DB_PASSWORD}@127.0.0.1:{DB_PORT}/{DB_NAME}"
    child_env = dict(os.environ, TEST_DATABASE_URL=url, DATABASE_URL=url)

    if not db_ok:
        failing.append("database")
        print("\nskipping pytest and the kb smoke because the database is not up")
    else:
        print("\n== pytest -q ==")
        pt = run([sys.executable, "-m", "pytest", "-q", "--tb=short"], env=child_env)
        if pt.returncode != 0:
            nodes = pytest_failures(pt.stdout)
            failing.append("pytest(" + ",".join(nodes) + ")" if nodes else "pytest")

        print("\n== kb smoke ==")
        for cmd in KB_SMOKE:
            print(f"-- kb {cmd} --")
            res = run([sys.executable, "-m", "kb", cmd], env=child_env)
            if res.returncode != 0:
                failing.append(f"kb-{cmd}")

    print("\n== no-literals guard ==")
    if (ROOT / "data" / "current").is_dir():
        print("data/current present: tests/test_kb_no_literals.py ran against real data")
    else:
        print("VACUOUS: no data/current mirror on this machine, so tests/test_kb_no_literals.py was skipped; "
              "run it where data/current exists before trusting the no-literals result")

    elapsed = time.monotonic() - start
    note = "" if elapsed <= TIME_BUDGET else f"  WARNING over the {int(TIME_BUDGET)}s budget"
    print(f"\nelapsed {elapsed:.0f}s (budget {int(TIME_BUDGET)}s){note}")

    if failing:
        print(f"CHECK FAIL {sha} {' '.join(failing)}")
        return 1
    print(f"CHECK PASS {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
