"use strict";
// ---------- rehearsal operator ----------
function renderOperator() {
  const op = $("operator"), r = S.state.rehearsal;
  if (!r || params.has("clean")) { op.hidden = true; return; }
  op.hidden = false;
  op.replaceChildren(
    el("span", {}, "Rehearsal ", el("b", {}, `${r.applied}/${r.beats}`)),
    el("span", {}, r.blocked_by ? `· ${r.blocked_by}` : r.next ? ["· next: ", el("b", {}, r.at || "")] : "· finished"),
    el("button", { class: "btn", onclick: advance, disabled: !r.next || !!r.blocked_by }, "Next ", el("kbd", {}, "J")),
    el("button", { class: "btn", onclick: resetRehearsal }, "Reset"));
}
async function advance() {
  try { await api("/api/lookahead/advance", {}); } catch (e) { toast(e.message, true); }
}
async function resetRehearsal() {
  if (!confirm("Start the rehearsal over? This clears the rehearsal log.")) return;
  try { await api("/api/lookahead/reset", {}); } catch (e) { toast(e.message, true); }
}
