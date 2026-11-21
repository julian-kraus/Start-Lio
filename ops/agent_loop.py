"""Run one agent role unattended: start a fresh headless session per round, log it, restart on exit,
back off on rate limits, wait when there is no work, stop on the stop signal.

usage: python3 ops/agent_loop.py --role worker|reviewer|coordinator --agent-id L1-W1 [--model M]
       [--idle-wait 10] [--round-timeout 100] [--stop-at 09:30] [--cli <agent command>]
Stop signals: the label `night:stop` on the status issue, a file ops/STOP, or the --stop-at time.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "julian-kraus/Start-Lio"
ROLE_SLEEP = {"worker": 0, "reviewer": 20, "coordinator": 30}   # minutes between rounds when work was done
NO_WORK = "NO_WORK"
RATE_HINTS = ("rate limit", "usage limit", "429", "overloaded")


def stop_requested(stop_at: dt.time) -> str | None:
    if (ROOT / "ops" / "STOP").exists():
        return "ops/STOP exists"
    if dt.datetime.now().time() >= stop_at and dt.datetime.now().hour < 18:
        return f"past {stop_at}"
    try:
        out = subprocess.run(["gh", "issue", "list", "-R", REPO, "--label", "night:stop", "--state", "open",
                              "--json", "number"], capture_output=True, text=True, timeout=60).stdout
        if json.loads(out or "[]"):
            return "label night:stop is set"
    except (subprocess.SubprocessError, json.JSONDecodeError):
        pass
    return None


def prompt_for(role: str, agent_id: str) -> str:
    text = (ROOT / "ops" / "prompts" / f"{role}.md").read_text()
    return text.replace("{AGENT_ID}", agent_id).replace("{NO_WORK}", NO_WORK)


def run_round(args, log_dir: Path) -> tuple[int, str]:
    cmd = [args.cli, "-p", prompt_for(args.role, args.agent_id), "--output-format", "json",
           "--permission-mode", "auto", "--settings", str(ROOT / "ops" / "agent-settings.json"),
           "--setting-sources", "project,local"]
    if args.model:
        cmd += ["--model", args.model]
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    log = log_dir / f"{stamp}.json"
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=args.round_timeout * 60)
        log.write_text(r.stdout + ("\n--- stderr ---\n" + r.stderr if r.stderr else ""))
        try:
            result = json.loads(r.stdout).get("result", "")
        except json.JSONDecodeError:
            result = r.stdout[-2000:] + r.stderr[-2000:]
        return r.returncode, result
    except subprocess.TimeoutExpired:
        log.write_text("round timed out")
        return 124, "round timed out"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", required=True, choices=sorted(ROLE_SLEEP))
    ap.add_argument("--agent-id", required=True)
    ap.add_argument("--model")
    ap.add_argument("--cli", default="claude")
    ap.add_argument("--idle-wait", type=float, default=10.0, help="minutes to wait when there is no work")
    ap.add_argument("--round-timeout", type=float, default=100.0, help="minutes before a round is killed")
    ap.add_argument("--stop-at", default="09:30")
    args = ap.parse_args()
    stop_at = dt.time.fromisoformat(args.stop_at)
    log_dir = ROOT / "logs" / "agents" / args.agent_id
    log_dir.mkdir(parents=True, exist_ok=True)
    heartbeat = log_dir / "heartbeat"
    while True:
        reason = stop_requested(stop_at)
        if reason:
            print(f"{args.agent_id}: stopping ({reason})", flush=True)
            return
        code, result = run_round(args, log_dir)
        heartbeat.write_text(json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"), "exit": code,
                                         "result": result[-500:]}))
        lowered = result.lower()
        if code != 0 and any(h in lowered for h in RATE_HINTS):
            wait = 20
        elif code != 0:
            wait = 2
        elif NO_WORK in result:
            wait = args.idle_wait
        else:
            wait = ROLE_SLEEP[args.role]
        print(f"{dt.datetime.now():%H:%M} {args.agent_id}: exit {code}, next round in {wait} min", flush=True)
        time.sleep(wait * 60)


if __name__ == "__main__":
    sys.exit(main())
