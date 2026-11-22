"use strict";
// ---------- other views ----------
function viewWeak() {
  const order = { critical: 0, at_risk: 1, hedged: 2, covered: 3, ok: 4 };
  const rows = [...lanes()].sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9));
  const v = el("div", { class: "view" }, el("h2", { class: "vt" }, "Weak spots ", el("small", {}, "parts that can break the goal, and why")));
  if (!rows.length) return v.appendChild(el("div", { class: "card emptytxt" }, S.state.goal ? "No plan yet." : "Set the goal first: weak spots are priced against it.")), v;
  v.append(el("div", { class: "card" }, el("table", { class: "list" },
    el("thead", {}, el("tr", {}, ["Part", "Edition", "Status", "Need", "Usable", "Gap", "Why", "Backup"].map((h) => el("th", {}, h)))),
    el("tbody", {}, rows.map((l) => el("tr", { class: "click", onclick: () => { S.hlLane = l.lane_id; setView("timeline"); select({ type: "lane", id: l.lane_id }); } },
      el("td", {}, el("b", {}, l.name), el("div", { class: "src" }, l.lane_id)),
      el("td", {}, l.edition || ""),
      el("td", {}, el("span", { class: "st " + l.status }, statusText(l.status))),
      el("td", { class: "num" }, num(l.need)), el("td", { class: "num" }, num(l.usable)),
      el("td", { class: "num" }, l.gap ? el("span", { class: "neg" }, "−" + num(l.gap)) : "0"),
      el("td", {}, l.reason || ""),
      el("td", {}, (l.bars || []).filter((b) => b.kind === "hedge").map((b) => b.label).join(", ") || "none")))))));
  return v;
}
