"use strict";
function viewDecisions() {
  const ds = [...S.state.decisions].sort((a, b) => (a.status === "open" ? -1 : 1) - (b.status === "open" ? -1 : 1));
  const v = el("div", { class: "view" }, el("h2", { class: "vt" }, "Decisions ", el("small", {}, "only what needs a person; everything else the agent does inside your limits")));
  if (!ds.length) return v.appendChild(el("div", { class: "card emptytxt" }, "Nothing to decide.")), v;
  v.append(el("div", { class: "dlist" }, ds.map((d) => el("div", { class: "card ditem" },
    el("div", {}, el("div", { class: "t" }, d.title), el("div", { class: "m" },
      d.status === "open" ? `${d.cost ? d.cost.text : ""} · signs: ${d.signer ? d.signer.name + ", " + d.signer.role : "you"}` :
        `${d.status} by ${d.decided_by || "?"} · ${fmt(d.decided_at)}`)),
    d.status === "open" ? el("button", { class: "btn primary", onclick: () => { S.decisionOpen = d.decision_id; render(); } }, "Review")
      : el("span", { class: "st " + d.status }, d.status)))));
  return v;
}
