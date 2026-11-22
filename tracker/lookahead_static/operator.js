"use strict";
// ---------- operator: rehearsal beats, or the scenario clock in live mode ----------
const CLOCK_POLL_MS = 2000, CLOCK_REDRAW_MS = 2 * 60 * 1000;
function renderOperator() {
  const op = $("operator"), r = S.state.rehearsal, c = S.state.clock;
  if (params.has("clean") || (!r && !c.simulated)) { op.hidden = true; return; }
  op.hidden = false;
  if (!r) {
    op.replaceChildren(
      el("span", {}, "Scenario clock ", c.next ? ["· next: ", el("b", {}, fmt(c.next))] : "· no later demo time"),
      el("button", { class: "btn", onclick: jumpClock, disabled: !c.next }, "Jump ", el("kbd", {}, "J")));
    return;
  }
  op.replaceChildren(
    el("span", {}, "Rehearsal ", el("b", {}, `${r.applied}/${r.beats}`)),
    el("span", {}, r.blocked_by ? `· ${r.blocked_by}` : r.next ? ["· next: ", el("b", {}, r.at || "")] : "· finished"),
    el("button", { class: "btn", onclick: advance, disabled: !r.next || !!r.blocked_by }, "Next ", el("kbd", {}, "J")),
    el("button", { class: "btn", onclick: resetRehearsal }, "Reset"));
}
async function advance() {
  try { await api("/api/lookahead/advance", {}); } catch (e) { toast(e.message, true); }
}
let jumping = false;
async function jumpClock() {
  if (jumping) return;
  jumping = true;
  try { await api("/api/lookahead/clock/next", {}); await load(); } catch (e) { toast(e.message, true); }
  jumping = false;
}
// The plan clock has no records, so live pages ask for it: a jump by anyone redraws every open page.
async function watchClock() {
  if (!S.state || S.state.mode !== "live" || jumping) return;
  let c;
  try { c = await api("/api/lookahead/clock"); } catch (e) { c = { ...S.state.clock, error: e.message }; }
  if (!S.state) return;
  const old = S.state.clock;
  if (c.simulated !== old.simulated || c.error !== old.error || Math.abs(ms(c.now) - S.clockLoaded) > CLOCK_REDRAW_MS) return load();
  S.state.clock = c;
  renderClock();
  if (c.next !== old.next) renderOperator();
}
async function resetRehearsal() {
  if (!confirm("Start the rehearsal over? This clears the rehearsal log.")) return;
  try { await api("/api/lookahead/reset", {}); } catch (e) { toast(e.message, true); }
}
