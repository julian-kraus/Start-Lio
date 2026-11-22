"""Lookahead: the launch timeline as an append-only record log, folded into one state for the web page.

Producers (the planning agent, the mail worker, a rehearsal) append JSON records to one log file, one per line.
The web server tails that file, folds every record into `state()` and pushes each new record to open pages.

Live mode shows only records whose `producer` is "agent", "agent:<role>" or "monitor", plus the human steps
taken on the page ("app"). Everything else in the log, a rehearsal above all, is counted and never shown.
Producers pass it on: append_records(path, records, producer="monitor"). The mode is the server's, never the
log's: only a server started with a rehearsal script rehearses, and that one never sends mail.

Record kinds:
  launch    name, deadline, product, mode ("live" | "rehearse"), demo_times[] (stops of the jump key)
  clock     now                                  rehearse mode only; live mode follows plan.clock
  goal      statement, answers[], unit_loss_eur, budget_cap_eur, sacrifices[], edition
  plan      lanes[], coverage {covered, needed, unit}, summary      latest solve replaces the previous one
  supplier  supplier_id, ...persona and score fields                upsert by supplier_id
  event     type, title, at, supplier_id?, lane?, severity?, impact?, source?, detail?   shown on the world lane
  decision  decision_id, status, ...                                upsert by decision_id
  promise   promise_id, state, ...                                  upsert by promise_id
  cue       label, wait_for?                                        rehearsal beat boundary, never shown

CLI:
  python -m tracker.lookahead append <log> <record.json | -> [producer]     append one record (or a JSON array)
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import queue
import sys
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .lookahead_view import plan_view, shown_live

log = logging.getLogger("tracker.lookahead")
KINDS = {"launch", "clock", "goal", "plan", "supplier", "event", "decision", "promise", "cue", "rules"}
UPSERT_KEYS = {"supplier": "supplier_id", "decision": "decision_id", "promise": "promise_id"}


class LookaheadError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def validate(record: dict) -> dict:
    if not isinstance(record, dict):
        raise LookaheadError(400, "record must be a JSON object")
    kind = record.get("kind")
    if kind not in KINDS:
        raise LookaheadError(400, f"unknown kind {kind!r}")
    key = UPSERT_KEYS.get(kind)
    if key and not record.get(key):
        raise LookaheadError(400, f"{kind} record needs {key}")
    if kind == "event" and not (record.get("type") and record.get("title")):
        raise LookaheadError(400, "event record needs type and title")
    return record


def append_records(path: Path, records: list[dict], producer: str | None = None) -> None:
    """Append records under an exclusive lock so concurrent producers never interleave lines.
    `producer` is stamped on records that do not name one."""
    for r in records:
        validate(r)
    if producer:
        records = [{"producer": producer, **r} for r in records]
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def fold(records: list[dict]) -> dict:
    """Pure fold of the record log into the page state. Records carry their 1-based log position as id."""
    st: dict = {"launch": None, "clock": None, "goal": None, "plan": None, "suppliers": {}, "events": [],
                "decisions": {}, "promises": {}, "last_id": 0, "beat": None, "rules": None}
    for r in records:
        kind = r["kind"]
        st["last_id"] = r["id"]
        if kind == "launch":
            st["launch"] = r
        elif kind == "clock":
            st["clock"] = r["now"]
        elif kind == "goal":
            st["goal"] = r
        elif kind == "plan":
            st["plan"] = r
        elif kind == "event":
            st["events"].append(r)
        elif kind == "cue":
            st["beat"] = r.get("label")
        elif kind == "rules":
            st["rules"] = r
        else:
            key = UPSERT_KEYS[kind]
            bucket = st[kind + "s"]
            merged = {**bucket.get(r[key], {}), **r}
            if kind == "supplier" and "score" in r:
                hist = list(bucket.get(r[key], {}).get("history", []))
                prev = bucket.get(r[key], {}).get("score")
                if prev is None or prev.get("value") != r["score"].get("value"):
                    hist.append({"value": r["score"].get("value"), "at": r.get("at"), "cause": r.get("cause"),
                                 "id": r["id"]})
                merged["history"] = hist
            bucket[r[key]] = merged
    return st


class Lookahead:
    """Tails the log file, keeps the folded state and fans new records out to SSE subscribers."""

    def __init__(self, log_path: Path, rehearsal: Path | None = None, clock=None):
        from .lookahead_clock import PlanClock  # that module needs LookaheadError from this one

        self.path = log_path
        self.rehearsal = rehearsal
        self.mode = "rehearse" if rehearsal else "live"
        self.clock = clock or PlanClock()
        self.records: list[dict] = []
        self.offset = 0
        self.lock = threading.Lock()
        self.read_lock = threading.Lock()  # one tailer at a time: the poll thread or a request that just appended
        self.clients: set[queue.Queue] = set()
        self._script = self._load_script(rehearsal) if rehearsal else []
        self._read_new()
        if self.rehearsal and not self.records:
            self.advance()

    # ---------- log tailing ----------

    def _read_new(self) -> list[dict]:
        with self.read_lock:
            return self._read_new_locked()

    def _read_new_locked(self) -> list[dict]:
        if not self.path.exists():
            if self.offset:  # the log was reset by another process
                self._reset_memory()
            return []
        size = self.path.stat().st_size
        if size < self.offset:
            self._reset_memory()
        new = []
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            chunk = f.read()
        complete, _, _ = chunk.rpartition(b"\n")
        if not complete and not chunk.endswith(b"\n"):
            return []
        for line in complete.split(b"\n"):
            if not line.strip():
                continue
            try:
                rec = validate(json.loads(line))
            except (ValueError, LookaheadError) as exc:
                log.warning("skipping bad record at byte %d: %s", self.offset, exc)
                continue
            with self.lock:
                rec = {**rec, "id": len(self.records) + 1}
                self.records.append(rec)
            new.append(rec)
        self.offset += len(complete) + 1
        return new

    def _reset_memory(self) -> None:
        with self.lock:
            self.records = []
            self.offset = 0
        self._broadcast({"kind": "reset", "id": 0})

    def run(self) -> None:
        while True:
            try:
                for rec in self._read_new():
                    self._broadcast(rec)
            except OSError as exc:
                log.warning("cannot read %s: %s", self.path, exc)
            time.sleep(0.25)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.clients.discard(q)

    def _shows(self, rec: dict) -> bool:
        return self.mode == "rehearse" or shown_live(rec)

    def _visible(self) -> list[dict]:
        return [r for r in self.records if self._shows(r)]

    def _broadcast(self, rec: dict) -> None:
        if rec["kind"] != "reset" and not self._shows(rec):
            return
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(rec)
                except queue.Full:
                    self.clients.discard(q)

    def since(self, after: int) -> list[dict]:
        with self.lock:
            return [r for r in self._visible() if r["id"] > after]

    # ---------- state ----------

    def state(self) -> dict:
        with self.lock:
            shown = self._visible()
            hidden = len(self.records) - len(shown)
            st = fold(deepcopy(shown))
        st["clock"] = self.clock_status(st["launch"], st["clock"])
        st["plan"] = plan_view(st["plan"], st["launch"])
        st["mode"] = self.mode
        st["hidden_records"] = hidden
        st["suppliers"] = list(st["suppliers"].values())
        st["decisions"] = list(st["decisions"].values())
        st["promises"] = list(st["promises"].values())
        st["rehearsal"] = self._rehearsal_status() if self.rehearsal else None
        return st

    def _latest(self, kind: str) -> dict:
        with self.lock:
            return next((r for r in reversed(self._visible()) if r["kind"] == kind), {})

    def now(self) -> str:
        """The time to stamp on a record. Fails rather than stamping real time during a scenario."""
        if self.mode == "rehearse":
            return self._latest("clock").get("now") or datetime.now(timezone.utc).isoformat()
        return self.clock.read()[0].isoformat()

    def clock_status(self, launch: dict | None, recorded: str | None = None) -> dict:
        real = datetime.now(timezone.utc).isoformat()
        if self.mode == "rehearse":
            return {"now": recorded or real, "simulated": True}
        from .lookahead_clock import next_stop

        try:
            now, simulated = self.clock.read()
        except LookaheadError as exc:
            return {"now": real, "simulated": False, "error": str(exc)}
        status = {"now": now.isoformat(), "simulated": simulated}
        if simulated:
            try:
                stop = next_stop((launch or {}).get("demo_times") or [], now)
                status["next"] = stop.isoformat() if stop else None
            except LookaheadError as exc:
                status["error"] = f"demo_times: {exc}"
        return status

    def live_clock(self) -> dict:
        return self.clock_status(self._latest("launch"))

    def move_clock(self, to=None) -> str:
        return self.clock.move(to, self._latest("launch").get("demo_times")).isoformat()

    def event_detail(self, event_id: int) -> dict | None:
        with self.lock:
            for r in self._visible():
                if r["id"] == event_id and r["kind"] == "event":
                    return deepcopy(r)
        return None

    def append(self, records: list[dict], producer: str | None = None) -> None:
        # unnamed, it is the page's own human step, which in a rehearsal is part of the rehearsal
        append_records(self.path, records, producer or ("app" if self.mode == "live" else "rehearsal"))
        for rec in self._read_new():
            self._broadcast(rec)

    # ---------- decisions ----------

    def decision(self, decision_id: str) -> dict | None:
        with self.lock:
            st = fold(deepcopy(self._visible()))
        return st["decisions"].get(decision_id)

    def record_decision(self, decision_id: str, status: str, by: str, now: str) -> None:
        self.append([{"kind": "decision", "decision_id": decision_id, "status": status, "decided_by": by,
                      "decided_at": now}])

    # ---------- rehearsal ----------

    @staticmethod
    def _load_script(path: Path) -> list[list[dict]]:
        """Split the rehearsal file into beats; each beat ends with (and includes) a cue record."""
        beats, cur = [], []
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rec = validate(json.loads(line))
            except (ValueError, LookaheadError) as exc:
                raise SystemExit(f"{path}:{n}: {exc}") from exc
            cur.append(rec)
            if rec["kind"] == "cue":
                beats.append(cur)
                cur = []
        if cur:
            beats.append(cur)
        return beats

    def _beats_applied(self) -> int:
        with self.lock:
            return sum(1 for r in self.records if r["kind"] == "cue")

    def _last_cue(self) -> dict:
        return self._latest("cue")

    def _rehearsal_status(self) -> dict:
        done = self._beats_applied()
        nxt = self._script[done] if done < len(self._script) else None
        return {"beats": len(self._script), "applied": done, "at": self._last_cue().get("label"),
                "next": nxt[-1].get("label") if nxt else None, "blocked_by": self._blocked_by(self._last_cue())}

    def _blocked_by(self, cue: dict) -> str | None:
        """The last applied cue may wait for a human step before the rehearsal can move on."""
        wait = cue.get("wait_for")
        if not wait:
            return None
        kind, _, ref = wait.partition(":")
        if kind == "goal":
            with self.lock:
                has_goal = any(r["kind"] == "goal" for r in self.records)
            return None if has_goal else "waiting for the goal interview"
        if kind == "accept":
            d = self.decision(ref)
            return None if d and d.get("status") == "accepted" else f"waiting for decision {ref}"
        return f"unknown wait_for {wait!r}"

    def advance(self, force: bool = False) -> dict:
        """Apply the next rehearsal beat. A cue that waits for a human step holds the rehearsal until it happens."""
        if not self.rehearsal:
            raise LookaheadError(409, "not in rehearse mode")
        done = self._beats_applied()
        if done >= len(self._script):
            raise LookaheadError(409, "rehearsal finished")
        blocked = self._blocked_by(self._last_cue())
        if blocked and not force:
            raise LookaheadError(409, blocked)
        self.append([{**r, "producer": "rehearsal"} for r in self._script[done]])
        return self._rehearsal_status()

    def after_human_step(self) -> None:
        """In rehearse mode a goal or an accepted decision releases the beat that was waiting for it."""
        if self.rehearsal and self._last_cue().get("wait_for") and not self._blocked_by(self._last_cue()) \
                and self._beats_applied() < len(self._script):
            self.advance()

    def reset(self) -> None:
        if not self.rehearsal:
            raise LookaheadError(409, "reset is only allowed in rehearse mode")
        with self.read_lock:
            if self.path.exists():
                self.path.unlink()
            self._reset_memory()
        self.advance()


def _cli(argv: list[str]) -> int:
    if len(argv) not in (3, 4) or argv[0] != "append":
        print(__doc__, file=sys.stderr)
        return 2
    raw = sys.stdin.read() if argv[2] == "-" else Path(argv[2]).read_text(encoding="utf-8")
    data = json.loads(raw)
    records = data if isinstance(data, list) else [data]
    try:
        append_records(Path(argv[1]), records, argv[3] if len(argv) == 4 else None)
    except LookaheadError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"appended {len(records)} record(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
