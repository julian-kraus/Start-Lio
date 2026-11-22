"use strict";
function viewSuppliers() {
  const v = el("div", { class: "view" }, el("h2", { class: "vt" }, "Suppliers ", el("small", {}, "who they are, how they behave, how far to trust them")));
  v.append(el("div", { class: "grid" }, S.state.suppliers.map((s) => el("div", { class: "card sup", onclick: () => select({ type: "supplier", id: s.supplier_id }) },
    el("div", { class: "hd" }, el("div", { class: "avatar" }, initials(s.name)),
      el("div", {}, el("b", {}, s.name), el("div", { class: "src" }, `${s.supplier_id} · ${s.city || ""} ${s.country || ""} · writes ${(s.lang || "").toUpperCase()}`)),
      scoreEl(s)),
    el("div", { class: "persona-txt" }, s.persona || ""),
    el("div", { class: "chips" }, (s.parts || []).map((p) => el("span", {}, p))),
    (s.flags || []).length ? el("div", { class: "flags" }, s.flags.map((f) => el("span", { class: "flag" }, f))) : null))));
  return v;
}
function scoreEl(s) {
  const v = s.score ? s.score.value : null;
  return el("div", { class: "score " + (v == null ? "" : scoreClass(v)) }, v == null ? "n/a" : Math.round(v * 100) + "%", el("small", {}, "reliability"));
}
