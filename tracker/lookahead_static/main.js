"use strict";
// ---------- keyboard and deep links ----------
document.addEventListener("keydown", (e) => {
  if (e.target.closest && e.target.closest("input, textarea, select")) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if ($("modal-root").firstChild && k !== "escape") return;
  if (k === "escape") {
    if ($("modal-root").firstChild) $("modal-root").replaceChildren();
    else if (S.decisionOpen) { S.dismissed.add(S.decisionOpen); S.decisionOpen = null; render(); }
    else select(null);
  } else if (k === "j" && S.state) {
    if (e.repeat) return;
    if (S.state.rehearsal) advance();
    else if (S.state.clock.simulated) jumpClock();
  }
  else if (k === "g") openGoal();
  else if (k === "d") { const d = openDecisions()[0]; if (d) { S.decisionOpen = d.decision_id; render(); } else setView("decisions"); }
  else if (k === "f") { S.range = S.range === "all" ? "launch" : "all"; render(); }
  else if (k === "t") setView("timeline");
  else if (k === "o") setView("overview");
});
function applyHash() {
  const h = location.hash.slice(1);
  let m;
  if ((m = h.match(/^event-(\d+)$/))) S.sel = { type: "event", id: Number(m[1]) };
  else if ((m = h.match(/^supplier-([\w.-]+)$/))) S.sel = { type: "supplier", id: m[1] };
  else if ((m = h.match(/^view-(\w+)$/)) && VIEWS.some(([k]) => k === m[1])) S.view = m[1];
}
window.addEventListener("resize", () => { clearTimeout(window.__rz); window.__rz = setTimeout(render, 120); });
window.addEventListener("hashchange", () => { applyHash(); render(); });
applyHash();
load().then(connect);
setInterval(watchClock, CLOCK_POLL_MS);
