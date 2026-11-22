"use strict";
function viewPromises() {
  const ps = [...S.state.promises].sort((a, b) => ms(a.next_check || a.due) - ms(b.next_check || b.due));
  const v = el("div", { class: "view" }, el("h2", { class: "vt" }, "Promises ", el("small", {}, "every promise has a check time, a proof and a fallback")));
  if (!ps.length) return v.appendChild(el("div", { class: "card emptytxt" }, "No open promises.")), v;
  v.append(el("div", { class: "card" }, el("table", { class: "list" },
    el("thead", {}, el("tr", {}, ["Supplier", "Promise", "Due", "Proof", "State", "If it breaks"].map((h) => el("th", {}, h)))),
    el("tbody", {}, ps.map((p) => el("tr", { class: "click", onclick: () => select({ type: "promise", id: p.promise_id }) },
      el("td", {}, (supplier(p.supplier_id) || {}).name || p.supplier_id),
      el("td", {}, p.what), el("td", {}, fmt(p.due)), el("td", {}, p.proof || ""),
      el("td", {}, el("span", { class: "st " + p.state }, p.state)), el("td", {}, p.fallback || "")))))));
  return v;
}
