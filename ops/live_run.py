"""Run a live LLM acceptance check for an issue without the caller ever seeing the API key.

usage: python ops/live_run.py <worktree> eval-extraction [--kind K ...] [--limit N]
       python ops/live_run.py <worktree> extract
       python ops/live_run.py <worktree> llm-eval [tests...]
       python ops/live_run.py --spend

<worktree> is the code under test, e.g. .worktrees/i7. Each run gets a fresh database on the local throwaway
Postgres (TEST_DATABASE_URL), loads the `data/current` mirror into it offline (no portal access), runs the kb
setup, then the command. The key comes from secrets/anthropic.env of this clone and is passed only to the child
process as LOOKAHEAD_ANTHROPIC_API_KEY; output is scrubbed of it. Answers are cached in logs/live/cache, shared
by all runs, so a rerun pays only for changed prompts. Runs are refused once the estimated spend reaches
LIVE_BUDGET_USD (default 100). Post counts and agreement only, never values from the data.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIRROR = ROOT / "data" / "current"
LIVE = ROOT / "logs" / "live"
CACHE = LIVE / "cache"
KEY_FILE = ROOT / "secrets" / "anthropic.env"
# USD per million tokens (input, output); override when the model or price changes
RATES = (float(os.environ.get("LIVE_RATE_IN", "1")), float(os.environ.get("LIVE_RATE_OUT", "5")))
BUDGET = float(os.environ.get("LIVE_BUDGET_USD", "100"))
COMMANDS = {"eval-extraction": ["-m", "kb", "eval-extraction"], "extract": ["-m", "kb", "extract"]}


def spend() -> float:
    tokens_in = tokens_out = 0
    for p in CACHE.rglob("*") if CACHE.exists() else []:
        if p.is_file():
            try:
                usage = json.loads(p.read_text()).get("usage") or {}
            except (ValueError, UnicodeDecodeError, AttributeError):
                continue
            tokens_in += usage.get("input_tokens", 0)
            tokens_out += usage.get("output_tokens", 0)
    return tokens_in / 1e6 * RATES[0] + tokens_out / 1e6 * RATES[1]


def read_key() -> str:
    for line in KEY_FILE.read_text().splitlines() if KEY_FILE.exists() else []:
        if line.startswith("LOOKAHEAD_ANTHROPIC_API_KEY="):
            key = line.split("=", 1)[1].strip().strip("'\"")
            if key:
                return key
    raise SystemExit("no LOOKAHEAD_ANTHROPIC_API_KEY in secrets/anthropic.env; ask a human to add it")


def base_env(db_url: str, data_dir: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.endswith("API_KEY") and k != "DATABASE_URL"}
    env.update(DATABASE_URL=db_url, TRACKER_DATA_DIR=str(data_dir), KB_LLM_CACHE_DIR=str(CACHE),
               SOONY_API_BASE_URL="http://127.0.0.1:9", SOONY_API_TOKEN="offline",
               KB_LLM_ADAPTER="kb.llm.anthropic:AnthropicClient", PYTHONUNBUFFERED="1")
    return env


def scrub(text: str, key: str) -> str:
    return text.replace(key, "[key]") if key else text


def offline_baseline(worktree: Path, env: dict) -> None:
    """Record a tracker baseline from the mirror with a client that can list but never download."""
    manifest = {}
    for p in sorted(MIRROR.rglob("*")):
        if p.is_file():
            data = p.read_bytes()
            manifest[str(p.relative_to(MIRROR))] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    mfile = Path(env["TRACKER_DATA_DIR"]) / "manifest.json"
    mfile.write_text(json.dumps(manifest))
    code = (
        "import json,sys\nfrom pathlib import Path\nfrom tracker import db, poller\n"
        "from tracker.client import RemoteFile\nfrom tracker.config import load_settings\n"
        "from tracker.store import BlobStore\n"
        "class C:\n def __init__(s,f): s.f=f\n def list_files(s): return s.f\n"
        " def __getattr__(s,n): raise RuntimeError('offline: '+n)\n"
        "m=json.loads(Path(sys.argv[1]).read_text()); f=[RemoteFile(p,v['sha256'],v['bytes']) for p,v in m.items()]\n"
        "s=load_settings(); st=BlobStore(s.data_dir)\n"
        "with db.connect(s.database_url) as conn:\n db.init_schema(conn); conn.commit()\n"
        " poller.import_local(st, Path(sys.argv[2]), f); poller.poll_once(s, C(f), st, conn, baseline=True)\n")
    step(worktree, env, ["-c", code, str(mfile), str(MIRROR)], "tracker baseline (offline)")


def step(worktree: Path, env: dict, args: list[str], label: str, key: str = "") -> subprocess.CompletedProcess:
    r = subprocess.run([sys.executable, *args], cwd=worktree, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        tail = scrub((r.stdout + r.stderr)[-3000:], key)
        raise SystemExit(f"{label} failed (exit {r.returncode}):\n{tail}")
    return r


def main() -> None:
    if sys.argv[1:2] == ["--spend"]:
        print(f"estimated live spend so far: ${spend():.2f} of ${BUDGET:.0f} budget")
        return
    if len(sys.argv) < 3 or sys.argv[2] not in (*COMMANDS, "llm-eval"):
        raise SystemExit(__doc__)
    worktree, cmd, extra = Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3:]
    if not (worktree / "kb").is_dir():
        raise SystemExit(f"{worktree} is not a checkout of this repo")
    if not MIRROR.is_dir():
        raise SystemExit("no data/current mirror in this clone; a human must copy it first")
    test_url = os.environ.get("TEST_DATABASE_URL", "")
    if not test_url or any(x in test_url for x in ("supabase", "pooler")):
        raise SystemExit("TEST_DATABASE_URL must point at the local throwaway Postgres")
    if (used := spend()) >= BUDGET:
        raise SystemExit(f"live budget used up (${used:.2f} of ${BUDGET:.0f}); ask a human")
    key = read_key()
    run_id = f"live_{time.strftime('%H%M%S')}_{uuid.uuid4().hex[:4]}"
    data_dir = LIVE / "runs" / run_id
    data_dir.mkdir(parents=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    import psycopg
    with psycopg.connect(test_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{run_id}"')
    db_url = re.sub(r"/[^/?]*(\?|$)", f"/{run_id}\\1", test_url.split("://", 1)[1], count=1)
    db_url = test_url.split("://", 1)[0] + "://" + db_url
    env = base_env(db_url, data_dir)
    offline_baseline(worktree, env)
    for sub in ("migrate", "seed-rules", "load", "sync"):
        step(worktree, env, ["-m", "kb", sub], f"kb {sub}")
    env["LOOKAHEAD_ANTHROPIC_API_KEY"] = key
    shim = None
    if cmd == "llm-eval":
        # llm_eval.py reads <root>/secrets/anthropic.env, <root>/data/current and <root>/structured: give it a
        # throwaway root whose key file exists only for the length of the run
        shim = data_dir / "root"
        (shim / "secrets").mkdir(parents=True)
        (shim / "data").mkdir()
        (shim / "data" / "current").symlink_to(MIRROR)
        (shim / "structured").symlink_to(worktree / "structured")
        keyfile = shim / "secrets" / "anthropic.env"
        keyfile.touch(mode=0o600)
        keyfile.write_text(f"LOOKAHEAD_ANTHROPIC_API_KEY={key}\n"
                           f"LOOKAHEAD_LLM_MODEL={os.environ.get('LOOKAHEAD_LLM_MODEL', 'claude-haiku-5-5')}\n")
        args = ["tests/live/llm_eval.py", str(shim), str(data_dir / "llm_eval.json"), *extra]
    else:
        args = [*COMMANDS[cmd], *extra]
    try:
        r = subprocess.run([sys.executable, *args], cwd=worktree, env=env, capture_output=True, text=True)
    finally:
        if shim:
            (shim / "secrets" / "anthropic.env").unlink(missing_ok=True)
    out = "\n".join(line for line in scrub(r.stdout + r.stderr, key).splitlines() if "HTTP Request:" not in line)
    print(out[-8000:])
    print(f"live run {run_id}: exit {r.returncode}; estimated spend now ${spend():.2f} of ${BUDGET:.0f}")
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
