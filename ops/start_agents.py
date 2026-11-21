"""Start every agent loop for this machine in the background, each under caffeinate, logging to logs/.

usage: python3 ops/start_agents.py [--workers 2] [--reviewers 1] [--coordinators 1] [--dry-run]
       python3 ops/start_agents.py --stop        (stops the loops this script started on this machine)
Agent ids are generated from the machine name (see ops/agent_loop.py), so the command is the same everywhere.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIDS = ROOT / "logs" / "agents" / "pids.json"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_loop import default_agent_id  # noqa: E402


def plan(workers: int, reviewers: int, coordinators: int) -> list[tuple[str, int]]:
    return ([("worker", i) for i in range(1, workers + 1)] + [("reviewer", i) for i in range(1, reviewers + 1)]
            + [("coordinator", i) for i in range(1, coordinators + 1)])


def stop() -> None:
    if not PIDS.exists():
        print("no loops recorded")
        return
    for agent_id, pid in json.loads(PIDS.read_text()).items():
        try:
            os.killpg(pid, signal.SIGTERM)
            print(f"stopped {agent_id} (pid {pid})")
        except ProcessLookupError:
            print(f"{agent_id} was not running")
    PIDS.unlink()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--reviewers", type=int, default=1)
    ap.add_argument("--coordinators", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stop", action="store_true")
    args = ap.parse_args()
    if args.stop:
        return stop()
    if not os.environ.get("TEST_DATABASE_URL"):
        raise SystemExit("set TEST_DATABASE_URL to a local throwaway Postgres first (see AGENTS.md)")
    if any(x in os.environ["TEST_DATABASE_URL"] for x in ("supabase", "pooler")):
        raise SystemExit("TEST_DATABASE_URL points at the live database; use a local throwaway Postgres")
    started = json.loads(PIDS.read_text()) if PIDS.exists() else {}
    caffeinate = shutil.which("caffeinate")
    (ROOT / "logs").mkdir(exist_ok=True)
    PIDS.parent.mkdir(parents=True, exist_ok=True)
    for role, index in plan(args.workers, args.reviewers, args.coordinators):
        agent_id = default_agent_id(role, index)
        cmd = ([caffeinate, "-dimsu"] if caffeinate else []) + [
            sys.executable, str(ROOT / "ops" / "agent_loop.py"), "--role", role, "--index", str(index)]
        if args.dry_run:
            print(agent_id, " ".join(cmd))
            continue
        if agent_id in started:
            print(f"{agent_id} already started (pid {started[agent_id]}); skip")
            continue
        out = open(ROOT / "logs" / f"{agent_id}.out", "a")
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        started[agent_id] = proc.pid
        print(f"started {agent_id} (pid {proc.pid}), log logs/{agent_id}.out")
    if not args.dry_run:
        PIDS.write_text(json.dumps(started, indent=1))


if __name__ == "__main__":
    main()
