"use strict";
// ---------- goal interview ----------
function openGoal() {
  const L = S.state.launch || {};
  const qs = L.goal_questions || [];
  if (!qs.length) { toast("This launch has no goal questions.", true); return; }
  const prev = (S.state.goal && S.state.goal.answers) || {};
  const inputs = {};
  const err = el("div", { class: "err", style: "color:var(--risk);font-size:13px" });
  const close = () => $("modal-root").replaceChildren();
  const submit = async (ev) => {
    ev.preventDefault();
    const answers = {};
    for (const q of qs) answers[q.id] = inputs[q.id].value.trim();
    try { await api("/api/lookahead/goal", { answers }); close(); toast("Goal set. Solving the plan."); }
    catch (e) { err.textContent = e.message; }
  };
  const form = el("form", { class: "modal", onsubmit: submit },
    el("h2", {}, "The goal"),
    el("p", { class: "sub" }, "Everything Lookahead does is priced against these answers. You can change them later; each change shows on the timeline."),
    qs.map((q) => {
      inputs[q.id] = el("input", { id: "q-" + q.id, value: prev[q.id] ?? q.suggested ?? "", inputmode: q.unit ? "decimal" : null, autocomplete: "off" });
      return el("div", { class: "q" }, el("label", { for: "q-" + q.id }, q.q, q.unit ? el("span", { class: "unit" }, ` (${q.unit})`) : null), inputs[q.id],
        q.why ? el("div", { class: "why" }, q.why) : null);
    }),
    err,
    el("div", { class: "acts", style: "display:flex;gap:10px;margin-top:8px" },
      el("button", { class: "btn primary big", type: "submit" }, "Set goal"),
      el("button", { class: "btn", type: "button", onclick: close }, "Cancel")));
  $("modal-root").replaceChildren(el("div", { class: "modal-back", onclick: (e) => { if (e.target === e.currentTarget) close(); } }, form));
  form.querySelector("button[type=submit]").focus();
}
