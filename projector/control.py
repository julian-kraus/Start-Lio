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


class Control:
    def __init__(self, world: World, scenario: dict, log: Path, base: Path, screen: str):
        self.world, self.scenario, self.log, self.base, self.screen = world, scenario, log, base, screen
        self.sent = 1
        all_beats = beats(build(world, scenario))
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in all_beats[0]))

    def steps(self) -> list[tuple[str, list[dict]]]:
        b = beats(build(self.world, self.scenario))
        return [(b[i - 1][-1].get("label", f"step {i}"), b[i]) for i in range(1, len(b))]

    def send(self, k: int) -> str:
        steps = self.steps()
        if not 1 <= k <= len(steps):
            return f"no step {k}"
        append_records(self.log, steps[k - 1][1], producer="lab")
        self.sent = max(self.sent, k + 1)
        return f"sent: {steps[k - 1][0]} ({len(steps[k - 1][1])} records)"

    def reset(self) -> str:
        req = urllib.request.Request(f"{self.screen}/api/lookahead/reset", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json", "Origin": self.screen})
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as exc:   # report it on the page; the screen may not be running
            return f"reset failed: {exc}"
        self.sent = 1
        return "reset: the screen starts again from the real data, waiting for the goal interview"

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
        rows = []
        for i, (label, recs) in enumerate(self.steps(), 1):
            done = i < self.sent
            titles = "; ".join(r.get("title") or r.get("kind") for r in recs if r["kind"] in ("event", "decision"))
            rows.append(f"<tr class='{'done' if done else ''}'><td>{i}</td><td><b>{html.escape(label)}</b>"
                        f"<div class='s'>{html.escape(titles)}</div></td><td><form method='post' action='/send/{i}'>"
                        f"<button>{'Send again' if done else 'Send'}</button></form></td></tr>")
        fields = "".join(
            f"<label>{html.escape(lbl)}<input name='{g}.{k}' value='{html.escape(str(self.scenario[g][k]))}'></label>"
            for g, k, lbl, _ in EDITABLE)
        body = f"""<!doctype html><html><head><meta charset="utf-8"><title>Scenario control</title><style>
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
<div class="card"><h1>Events</h1><table>{''.join(rows)}</table></div>
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
