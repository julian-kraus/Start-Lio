"""Scenario control: a separate local web page that sends scenario events to the Lookahead screen and resets it.

    python -m projector control --log data/lab/log.jsonl --base data/lab/base.jsonl --screen http://127.0.0.1:8791
    python -m tracker.web --port 8791 --rehearse data/lab/base.jsonl --lookahead-log data/lab/log.jsonl

Every event is recomputed from the seeded kb with the parameters on the page (sourcing/ does the math), then its
records are appended to the record log the screen follows. Reset asks the screen to start over from the base beat.
Local only: it binds to 127.0.0.1 and never sends mail; the screen in rehearse mode only records decisions.
"""
from __future__ import annotations

import copy
import html
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tracker.lookahead import append_records

from .artbook import build
from .world import World

EDITABLE = [("offer", "units", "Supplier A offers (units)", int), ("offer", "unit_price_eur", "Supplier A price (EUR)", float),
            ("backup", "units", "Backup lot (units)", int), ("backup", "deposit", "Backup deposit", str),
            ("goal", "unit_loss_eur", "Loss per missing copy (EUR, goal answer)", float),
            ("cancellation", "reason", "Cancellation reason", str)]


def beats(records: list[dict]) -> list[list[dict]]:
    out, cur = [], []
    for r in records:
        cur.append(r)
        if r["kind"] == "cue":
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


# The control page sends only what the world does. What the agent does in reply (weak spots once the goal is set,
# the decision card once A cancels, the outcome once a person accepts) follows automatically, as in the product.
# Beat indices of the art-book script: 0 base, 1 weak spots, 2 offer + hedge, 3 reply, 4 cancellation,
# 5 decision card, 6 outcome after the accept.
WORLD = [("The outside supplier offers the missing units", [2]), ("The backup supplier confirms the truck", [3]),
         ("The outside supplier cancels", [4, 5])]
AUTO = [("goal", "You set the goal on the screen", "the agent ranks the weak spots and plans", 1),
        ("accept", "You accept the decision on the screen", "the plan updates and the outcome is shown", 6)]


def sent_beats(script: list[list[dict]], state: dict) -> set[int]:
    """The beats the screen already shows. A beat without events (the decision) is known by its decision id, so it
    is never sent again over a person's decision."""
    titles = {e.get("title") for e in state.get("events") or []}
    decided = {d.get("decision_id") for d in state.get("decisions") or []}
    sent = {0}
    for i, beat in enumerate(script):
        events = {r["title"] for r in beat if r["kind"] == "event"}
        ids = {r["decision_id"] for r in beat if r["kind"] == "decision"}
        if i and ((events and events <= titles) or (ids and ids <= decided)):
            sent.add(i)
    return sent


class Control:
    def __init__(self, world: World, scenario: dict, log: Path, base: Path, screen: str):
        self.world, self.scenario, self.log, self.base, self.screen = world, scenario, log, base, screen
        self.done: set[int] = {0}
        self.lock = threading.Lock()
        all_beats = beats(build(world, scenario))
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in all_beats[0]))
        threading.Thread(target=self._follow, daemon=True).start()

    def _beats(self) -> list[list[dict]]:
        return beats(build(self.world, self.scenario))

    def _append(self, idx: list[int]) -> int:
        b, n = self._beats(), 0
        for i in idx:
            if i not in self.done:
                append_records(self.log, b[i], producer="lab")
                self.done.add(i)
                n += len(b[i])
        return n

    def _state(self) -> dict:
        with urllib.request.urlopen(f"{self.screen}/api/lookahead/state", timeout=5) as r:
            return json.loads(r.read())

    def _follow(self) -> None:
        """Add the agent's replies to what a person did on the screen: the goal, an accepted decision."""
        while True:
            try:
                s = self._state()
                with self.lock:
                    # what the screen already shows counts as sent, whoever sent it (a restart, another page)
                    self.done = sent_beats(self._beats(), s)
                    if s.get("goal") and 1 not in self.done:
                        self._append([1])
                    if any(d.get("status") == "accepted" for d in s.get("decisions") or []) and 6 not in self.done:
                        self._append([6])
            except Exception:   # the screen may be restarting; try again
                pass
            time.sleep(1)

    def send(self, k: int) -> str:
        if not 1 <= k <= len(WORLD):
            return f"no event {k}"
        label, idx = WORLD[k - 1]
        with self.lock:
            if 1 not in self.done:
                return "set the goal on the screen first: the agent needs it to rank the weak spots"
            before = [i for j, (_, ix) in enumerate(WORLD) if j < k - 1 for i in ix]
            if any(i not in self.done for i in before):
                return f"send the events above first: {WORLD[k - 2][0]}"
            if all(i in self.done for i in idx):
                return f"already sent: {label}"
            n = self._append(idx)
        return f"sent: {label} ({n} records)"

    def reset(self) -> str:
        req = urllib.request.Request(f"{self.screen}/api/lookahead/reset", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json", "Origin": self.screen})
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as exc:   # report it on the page; the screen may not be running
            return f"reset failed: {exc}"
        with self.lock:
            self.done = {0}
        return "reset: the screen starts again from the real data, waiting for the goal"

    def set_params(self, form: dict) -> str:
        scn = copy.deepcopy(self.scenario)
        for group, key, _, kind in EDITABLE:
            name = f"{group}.{key}"
            if name in form and str(form[name]).strip() != "":
                scn[group][key] = kind(form[name])
        build(self.world, scn)   # raises on inputs the models refuse, before anything is changed
        self.scenario = scn
        return "parameters saved; the next event uses them"

    def page(self, note: str = "") -> bytes:
        b, rows = self._beats(), []
        for i, (label, idx) in enumerate(WORLD, 1):
            done = all(x in self.done for x in idx)
            titles = "; ".join(r.get("title") or r.get("kind") for x in idx for r in b[x]
                               if r["kind"] in ("event", "decision"))
            rows.append(f"<tr class='{'done' if done else ''}'><td>{i}</td><td><b>{html.escape(label)}</b>"
                        f"<div class='s'>{html.escape(titles)}</div></td><td><form method='post' action='/send/{i}'>"
                        f"<button{' disabled' if done else ''}>{'Sent' if done else 'Send'}</button></form></td></tr>")
        auto = "".join(f"<tr class='{'done' if beat in self.done else ''}'><td></td><td><b>{html.escape(who)}</b>"
                       f"<div class='s'>then {html.escape(then)}"
                       f"{' (done)' if beat in self.done else ' (automatic)'}</div></td><td></td></tr>"
                       for _, who, then, beat in AUTO)
        fields = "".join(
            f"<label>{html.escape(lbl)}<input name='{g}.{k}' value='{html.escape(str(self.scenario[g][k]))}'></label>"
            for g, k, lbl, _ in EDITABLE)
        body = f"""<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="4;url=/"><title>Scenario control</title><style>
body{{font:15px/1.5 Inter,system-ui,sans-serif;background:#f6f7f9;color:#191c26;margin:0;padding:24px}}
.card{{background:#fff;border:1px solid #e3e6ec;border-radius:12px;padding:20px;max-width:920px;margin:0 auto 16px}}
h1{{font-size:20px;margin:0 0 4px}} .s{{color:#565b6f;font-size:13px}} table{{width:100%;border-collapse:collapse}}
td{{padding:10px 8px;border-top:1px solid #e3e6ec;vertical-align:top}} tr.done td{{opacity:.55}}
button{{background:#4d4b9e;color:#fff;border:0;border-radius:8px;padding:8px 14px;font:inherit;cursor:pointer}}
.reset{{background:#d5544a}} label{{display:block;margin:6px 0;font-size:13px;color:#565b6f}}
input{{display:block;width:100%;padding:6px 8px;border:1px solid #e3e6ec;border-radius:6px;font:inherit}}
.note{{background:rgba(77,75,158,.12);padding:8px 12px;border-radius:8px}}</style></head><body>
<div class="card"><h1>Scenario control</h1><div class="s">Sends scenario events to the Lookahead screen
(<a href="{self.screen}/lookahead" target="_blank">{self.screen}/lookahead</a>). Every number is computed from the
real data in kb; only the values below are scenario inputs.</div>
{f'<p class="note">{html.escape(note)}</p>' if note else ''}
<form method="post" action="/reset"><button class="reset">Reset to the real data</button></form></div>
<div class="card"><h1>World events</h1><div class="s">What suppliers do. The agent's replies appear on the screen
by themselves.</div><table>{''.join(rows)}</table></div>
<div class="card"><h1>On the screen</h1><table>{auto}</table></div>
<div class="card"><h1>Scenario inputs</h1><form method="post" action="/params">{fields}<button>Save</button></form></div>
</body></html>"""
        return body.encode()


def serve(control: Control, port: int) -> None:
    class H(BaseHTTPRequestHandler):
        def _page(self, note=""):
            data = control.page(note)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._page()

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode() if n else ""
            from urllib.parse import parse_qs
            form = {k: v[0] for k, v in parse_qs(raw).items()}
            try:
                if self.path == "/reset":
                    note = control.reset()
                elif self.path.startswith("/send/"):
                    note = control.send(int(self.path.rsplit("/", 1)[1]))
                elif self.path == "/params":
                    note = control.set_params(form)
                else:
                    note = "unknown action"
            except Exception as exc:   # show model refusals and bad inputs on the page instead of a 500
                note = f"refused: {exc}"
            self._page(note)

        def log_message(self, *a):
            pass

    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
