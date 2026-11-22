"use strict";
// ---------- goal interview ----------
// Two steps: type the answers, see how Lookahead read them back, then confirm by name. Nothing is stored until
// the confirm step, which posts the author; the read-back step posts only the answers.
function openGoal() {
  const L = S.state.launch || {};
  const qs = L.goal_questions || [];
  if (!qs.length) { toast("This launch has no goal questions.", true); return; }
  const prev = (S.state.goal && S.state.goal.answers) || {};
  const inputs = {};
  const err = el("div", { class: "err", style: "color:var(--risk);font-size:13px" });
  const close = () => $("modal-root").replaceChildren();
  const render = (form) => {
    $("modal-root").replaceChildren(el("div", { class: "modal-back", onclick: (e) => { if (e.target === e.currentTarget) close(); } }, form));
  };
  let answers = null;

  const ask = () => {
    err.textContent = "";
    const form = el("form", { class: "modal", onsubmit: review },
      el("h2", {}, "The goal"),
      el("p", { class: "sub" }, "Everything Lookahead does is priced against these answers. You can change them later; each change shows on the timeline."),
      qs.map((q) => {
        inputs[q.id] = el("input", { id: "q-" + q.id, value: (answers && answers[q.id]) ?? prev[q.id] ?? q.suggested ?? "", autocomplete: "off" });
        return el("div", { class: "q" }, el("label", { for: "q-" + q.id }, q.q), inputs[q.id],
          q.why ? el("div", { class: "why" }, q.why) : null);
      }),
      err,
      el("div", { class: "acts", style: "display:flex;gap:10px;margin-top:8px" },
        el("button", { class: "btn primary big", type: "submit" }, "Review goal"),
        el("button", { class: "btn", type: "button", onclick: close }, "Cancel")));
    render(form);
    form.querySelector("input").focus();
  };

  const review = async (ev) => {
    ev.preventDefault();
    answers = {};
    for (const q of qs) answers[q.id] = inputs[q.id].value.trim();
    try {
      const r = await api("/api/lookahead/goal", { answers });
      confirmStep(r.show || []);
    } catch (e) { err.textContent = e.message; }
  };

  const confirmStep = (shown) => {
    err.textContent = "";
    const name = el("input", { id: "goal-author", placeholder: "Your name", autocomplete: "name" });
    const set = async (ev) => {
      ev.preventDefault();
      const author = name.value.trim();
      if (!author) { err.textContent = "Add your name to set the goal."; return; }
      try { await api("/api/lookahead/goal", { answers, author }); close(); toast("Goal set. Solving the plan."); }
      catch (e) { err.textContent = e.message; }
    };
    const form = el("form", { class: "modal", onsubmit: set },
      el("h2", {}, "Confirm the goal"),
      el("p", { class: "sub" }, "This is how Lookahead read your answers. Set it if it is right, or go back to change it."),
      el("ul", { style: "margin:0 0 16px;padding-left:20px;line-height:1.5" }, shown.map((line) => el("li", {}, line))),
      el("div", { class: "q" }, el("label", { for: "goal-author" }, "Confirmed by"), name),
      err,
      el("div", { class: "acts", style: "display:flex;gap:10px;margin-top:8px" },
        el("button", { class: "btn primary big", type: "submit" }, "Set goal"),
        el("button", { class: "btn", type: "button", onclick: ask }, "Back")));
    render(form);
    name.focus();
  };

  ask();
}
