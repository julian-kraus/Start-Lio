"use strict";
// ---------- drawer ----------
function select(sel) {
  S.sel = sel;
  if (sel && sel.type === "event") history.replaceState(null, "", "#event-" + sel.id);
  else if (sel && sel.type === "supplier") history.replaceState(null, "", "#supplier-" + sel.id);
  render();
}
function renderDrawer() {
  const d = $("drawer");
  const content = S.sel && drawerContent(S.sel);
  if (!content) { d.hidden = true; d.replaceChildren(); return; }
  d.hidden = false;
  d.replaceChildren(...content);
}
function head(kind, title, meta, statusCls) {
  return el("div", { class: "drawer-head" },
    el("div", { class: "row" }, el("span", { class: "st " + (statusCls || "none") }, kind),
      el("button", { class: "x", title: "Close (Esc)", "aria-label": "Close", onclick: () => select(null) }, "×")),
    el("h3", {}, title), meta ? el("div", { class: "meta" }, meta) : null);
}
function sec(title, ...kids) { return el("section", {}, el("h4", {}, title), ...kids); }
function personaChip(id) {
  const s = supplier(id);
  if (!s) return null;
  return el("div", { class: "persona", onclick: () => select({ type: "supplier", id }) },
    el("div", { class: "avatar" }, initials(s.name)),
    el("div", {}, el("div", { class: "nm" }, s.name), el("div", { class: "ps" }, s.persona || "")),
    scoreEl(s));
}
function autonomy(a) {
  if (!a || !a.text) return null;
  const mark = a.mode === "needs_decision" ? "!" : a.mode === "autonomous" ? "✓" : "·";
  return el("div", { class: "autonomy " + a.mode }, el("span", { class: "ic" }, mark), el("span", {}, a.text));
}
const TYPE_LABEL = { email_out: "Email sent", email_in: "Email in", document: "Document", offer_in: "Offer", delay: "Delay",
  cancellation: "Cancellation", check_in: "Check-in", decision: "Decision", goal: "Goal", agent: "Agent" };
function drawerContent(sel) {
  const st = S.state;
  if (sel.type === "event") {
    const e = st.events.find((v) => v.id === sel.id);
    if (!e) return null;
    const dt = e.detail || {};
    const out = [head(TYPE_LABEL[e.type] || e.type, e.title, fmt(e.at), e.severity === "risk" ? "risk" : e.type === "agent" ? "planned" : "none")];
    if (e.impact && (e.impact.eur || e.impact.units)) out.push(el("section", {}, el("div", { class: "impact" },
      e.impact.eur ? el("div", { class: e.severity === "risk" ? "risk" : "" }, el("b", {}, eur(e.impact.eur)), el("span", {}, e.severity === "risk" ? "at stake" : "committed")) : null,
      e.impact.units ? el("div", {}, el("b", {}, num(e.impact.units)), el("span", {}, "copies")) : null)));
    if (dt.autonomy && dt.autonomy.text) out.push(el("section", {}, autonomy(dt.autonomy)));
    if (dt.happened || dt.source) out.push(sec("What happened", el("p", {}, dt.happened || ""),
      dt.source ? el("div", { class: "src" }, `${dt.source.label}: ${dt.source.path || ""}`) : null,
      dt.original ? el("details", { class: "orig" }, el("summary", {}, `Original (${dt.original.lang})`), el("div", {}, dt.original.text)) : null));
    if ((dt.actions || []).length) out.push(sec("What the agent did", dt.actions.map((a) => el("div", { class: "action" },
      el("div", { class: "top" }, el("span", { class: "t" }, a.title), el("span", { class: "st " + (a.status || "none") }, a.status || "")),
      a.to ? el("div", { class: "to" }, `To ${a.to}${a.lang ? " · in " + a.lang.toUpperCase() : ""}`) : null,
      a.body ? el("div", { class: "body" }, a.body) : null))));
    if ((dt.reasoning || []).length) out.push(sec("Why", el("ol", { class: "why" }, dt.reasoning.map((r) => el("li", {},
      el("span", {}, r.text), r.value ? el("span", { class: "v" }, r.value) : null,
      r.note || r.source ? el("span", { class: "n" }, [r.note, r.source].filter(Boolean).join(" · ")) : null)))));
    if ((dt.alternatives || []).length) out.push(sec("Alternatives considered", dt.alternatives.map((a) => el("div", { class: "alt" }, el("b", {}, a.option), el("span", {}, a.why_not)))));
    if (e.decision_id) {
      const d = st.decisions.find((x) => x.decision_id === e.decision_id);
      if (d && d.status === "open") out.push(el("section", {}, el("button", { class: "btn primary", onclick: () => { S.decisionOpen = d.decision_id; render(); } }, "Open the decision ", el("kbd", {}, "D"))));
    }
    if (e.supplier_id && supplier(e.supplier_id)) out.push(sec("Who", personaChip(e.supplier_id)));
    return out;
  }
  if (sel.type === "supplier") {
    const s = supplier(sel.id);
    if (!s) return null;
    const sc = s.score || {};
    const out = [head("Supplier", s.name, `${s.supplier_id} · ${s.city || ""}, ${s.country || ""}`, "none")];
    out.push(el("section", {}, el("div", { class: "persona", style: "cursor:default" }, el("div", { class: "avatar" }, initials(s.name)),
      el("div", {}, el("div", { class: "nm" }, s.contact || ""), el("div", { class: "ps" }, `writes ${(s.lang || "").toUpperCase()}`)), scoreEl(s))));
    out.push(sec("Persona", el("p", {}, s.persona || ""), (s.flags || []).length ? el("div", { class: "flags", style: "margin-top:8px" }, s.flags.map((f) => el("span", { class: "flag" }, f))) : null));
    out.push(sec("How the score is built", el("p", { style: "font-size:13px" }, sc.method || ""),
      el("dl", { class: "kv", style: "margin-top:10px" },
        (sc.breakdown || []).flatMap((b) => [el("dt", {}, b.label), el("dd", {}, b.value, b.source ? el("div", { class: "src" }, b.source) : null)]),
        el("dt", {}, "Soony average"), el("dd", {}, sc.prior != null ? Math.round(sc.prior * 100) + "%" : "n/a"),
        el("dt", {}, "On record"), el("dd", {}, sc.n != null ? `${sc.n} ${sc.n === 1 ? "order" : "orders"}` : "n/a"))));
    if ((s.history || []).length > 1) out.push(sec("Score changes", el("ol", { class: "why" }, s.history.map((h) => el("li", {},
      el("span", {}, fmt(h.at)), el("span", { class: "v " + scoreClass(h.value) }, Math.round(h.value * 100) + "%"), el("span", { class: "n" }, h.cause || ""))))));
    out.push(sec("Parts", el("div", { class: "chips" }, (s.parts || []).map((p) => el("span", {}, p)))));
    const ps = st.promises.filter((p) => p.supplier_id === s.supplier_id);
    if (ps.length) out.push(sec("Promises", ps.map((p) => el("div", { class: "alt" }, el("b", {}, p.what), el("span", {}, `${fmt(p.due)} · `, el("span", { class: "st " + p.state }, p.state))))));
    const evs = st.events.filter((e) => e.supplier_id === s.supplier_id).slice(-6).reverse();
    if (evs.length) out.push(sec("Recent events", evs.map((e) => el("div", { class: "alt", style: "cursor:pointer", onclick: () => select({ type: "event", id: e.id }) }, el("b", {}, e.title), el("span", {}, fmt(e.at))))));
    return out;
  }
  if (sel.type === "promise") {
    const p = st.promises.find((x) => x.promise_id === sel.id);
    if (!p) return null;
    return [head("Promise", p.what, (supplier(p.supplier_id) || {}).name || p.supplier_id, p.state),
      sec("Check", el("dl", { class: "kv" }, el("dt", {}, "Due"), el("dd", {}, fmt(p.due)), el("dt", {}, "Next check"), el("dd", {}, fmt(p.next_check)),
        el("dt", {}, "Proof"), el("dd", {}, p.proof || ""), el("dt", {}, "State"), el("dd", {}, el("span", { class: "st " + p.state }, p.state)),
        el("dt", {}, "If it breaks"), el("dd", {}, p.fallback || ""))),
      p.supplier_id ? sec("Who", personaChip(p.supplier_id)) : null].filter(Boolean);
  }
  if (sel.type === "lane" || sel.type === "bar") {
    const l = lanes().find((x) => x.lane_id === (sel.lane || sel.id));
    if (!l) return null;
    const out = [];
    const b = sel.type === "bar" && (l.bars || []).find((x) => x.id + x.kind === sel.id);
    if (b) {
      out.push(head(b.kind === "ghost" ? "If started now" : b.kind === "hedge" ? "Backup" : "Supply", b.label, `${num(b.qty)} pcs · ${l.name}`, b.state));
      out.push(sec("Timing", el("table", { class: "legs" }, (b.legs || []).map((g) => el("tr", {}, el("td", {}, g.label), el("td", {}, `${fmt(g.from)} → ${hm(g.to)}`))),
        el("tr", {}, el("td", {}, el("b", {}, "Usable")), el("td", {}, el("b", {}, fmt(b.usable_at)))),
        l.needed_by ? el("tr", {}, el("td", {}, "Needed by"), el("td", { style: ms(b.usable_at) > ms(l.needed_by) ? "color:var(--risk);font-weight:700" : "" }, fmt(l.needed_by))) : null)));
      if (b.supplier_id) out.push(sec("Who", personaChip(b.supplier_id)));
    } else {
      out.push(head("Part", l.name, `${l.lane_id} · ${l.edition || ""}`, l.status));
    }
    out.push(sec("Coverage", el("dl", { class: "kv" }, el("dt", {}, "Need"), el("dd", {}, num(l.need)), el("dt", {}, "Usable"), el("dd", {}, num(l.usable)),
      el("dt", {}, "Gap"), el("dd", {}, l.gap ? el("span", { class: "neg" }, "−" + num(l.gap)) : "0"),
      l.needed_by ? [el("dt", {}, "Needed by"), el("dd", {}, fmt(l.needed_by), el("div", { class: "src" }, l.needed_by_why || ""))] : null),
      el("p", { style: "margin-top:10px" }, l.reason || "")));
    const evs = st.events.filter((e) => e.lane === l.lane_id).slice(-6).reverse();
    if (evs.length) out.push(sec("Events on this part", evs.map((e) => el("div", { class: "alt", style: "cursor:pointer", onclick: () => select({ type: "event", id: e.id }) }, el("b", {}, e.title), el("span", {}, fmt(e.at))))));
    return out;
  }
  return null;
}
