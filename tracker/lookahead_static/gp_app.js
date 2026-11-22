// Grand Plan Lio screen: renders /api/lookahead/state in the prototype's layout (analysis/design/grand-plan-lio).
// Every number shown comes from the Lookahead records (computed by the projector from kb and sourcing/).
"use strict";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const ic = (n, c) => `<svg class="ic ${c || ""}" viewBox="0 0 24 24" aria-hidden="true">${(window.GP_ICONS || {})[n] || ""}</svg>`;
const hydrate = root => (root || document).querySelectorAll("[data-i]").forEach(e => {
  if (!e.dataset.done) { e.insertAdjacentHTML("afterbegin", ic(e.dataset.i, e.dataset.c)); e.dataset.done = 1; }
});
const eur = x => x == null ? "—" : "€" + Math.round(Number(x)).toLocaleString("en-US");
const num = x => x == null ? "—" : Math.round(Number(x)).toLocaleString("en-US");
const ms = t => new Date(t).getTime();
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
let TZ = "Europe/Berlin";
const parts = t => Object.fromEntries(new Intl.DateTimeFormat("en-GB", {timeZone: TZ, weekday: "short", hour: "2-digit",
  minute: "2-digit", hour12: false}).formatToParts(new Date(t)).map(p => [p.type, p.value]));
const hm = t => { const p = parts(t); return `${p.hour}:${p.minute}`; };
const dhm = t => { const p = parts(t); return `${p.weekday} ${p.hour}:${p.minute}`; };
const B = (c, t) => `<span class="badge b-${c}"><span class="dot"></span>${esc(t)}</span>`;
const day = t => new Intl.DateTimeFormat("en-GB", {timeZone: TZ, weekday: "short", day: "2-digit", month: "short"}).format(new Date(t));
const when = e => e.date_only ? day(e.at) : dhm(e.at);   // kb has some facts as a date, without a time
function srcText(refs) {   // where a number comes from, as its record says
  const all = [].concat(refs || []), facts = all.filter(r => r.fact_id).map(r => r.fact_id);
  const shown = facts.length ? [`kb facts ${facts.slice(0, 2).join(", ")}${facts.length > 2 ? ` and ${facts.length - 2} more` : ""}`] : [];
  all.filter(r => !r.fact_id).forEach(r => shown.push(r.scenario_input ? `scenario input: ${r.scenario_input}` : r.goal_line ? `goal: ${r.goal_line}` : r.label || ""));
  const text = [...new Set(shown.filter(Boolean))].join("; ");
  return text ? `Source: ${text}` : "Source not on the record";
}
const srcTip = refs => ` title="${esc(srcText(refs))}"`;
const srcLine = refs => `<small class="xs subtle" style="display:block;overflow-wrap:anywhere">${esc(srcText(refs))}</small>`;
const whyRow = r => `<div><dt>${esc(r.text)}</dt><dd>${esc(r.value || "")}${r.sources ? srcLine(r.sources) : ""}</dd></div>`;
const initials = n => String(n || "?").split(/\s+/).filter(Boolean).map(w => w[0]).slice(0, 2).join("").toUpperCase();
const tone = v => v >= 0.75 ? "tone-g" : v >= 0.55 ? "tone-am" : "tone-r";

let S = null, VIEW = "timeline";
const EV = {  // record type -> chip class and icon, as in the prototype
  offer_in: ["", "inbox"], agent: ["agent", "sparkles"], email_out: ["agent", "send"], email_in: ["good", "mail"],
  check_in: ["good", "check"], cancellation: ["bad", "x"], decision: ["agent", "gavel"], document: ["", "document"],
  delay: ["", "truck"], delivery: ["muted", "truck"],
};
const FEED_IC = {cancellation: "alert-triangle", email_in: "mail", email_out: "send", check_in: "check", goal: "check",
  agent: "sparkles", decision: "gavel", offer_in: "inbox"};

function supName(id) { const s = (S.suppliers || []).find(x => x.supplier_id === id); return s ? s.name : id; }
function openDecisions() { return (S.decisions || []).filter(d => d.status === "open"); }

// ---------- timeline ----------
const HOUR = 3600e3, MIN_CHIP = 120;
function range() {   // the prototype's window: 23 h up to the deadline, 2 h after it per 23 h shown
  const dead = ms(S.launch.deadline);
  const ats = (S.events || []).map(e => ms(e.at)).filter(t => t < dead).concat([ms(S.clock.now)]);
  const start = Math.min(dead - 23 * HOUR, Math.min(...ats) - HOUR / 2);
  return [start, dead + (dead - start) * 2 / 23];
}
function ticks(a, b) {   // local whole hours, a step from the prototype's 4 h upwards, at most 7 labels
  const step = [4, 6, 8, 12, 24].find(h => (b - a) / (h * HOUR) <= 7) || 24;
  let t = Math.ceil(a / HOUR) * HOUR;
  while (t < b && (+parts(t).hour % step || +parts(t).minute)) t += HOUR;
  const out = [];
  for (; t < b - (b - a) * .04; t += step * HOUR) out.push(t);   // no label cut off at the right edge
  return out;
}
function placeRows(items) {   // first row where the chip fits at its shortest; it then grows up to the next chip
  const rows = [];
  [...items].sort((a, b) => a.from - b.from).forEach(it => {
    const short = it.win ? it.to : it.from + Math.min(it.w, MIN_CHIP);
    let row = rows.findIndex(r => r.end + 4 <= it.from);
    if (row < 0) { row = rows.length; rows.push({end: 0, items: []}); }
    rows[row].end = short; rows[row].items.push(it); it.row = row;
  });
  rows.forEach(r => r.items.forEach((it, i) => {
    const next = r.items[i + 1];
    if (next && !it.win && !it.cls.includes("end")) it.w = Math.max(Math.min(it.w, MIN_CHIP), Math.min(it.w, next.from - it.from - 4));
  }));
  return Math.max(1, rows.length);
}
function criticalSuppliers() {   // suppliers of the parts the plan marks critical, and of open decisions
  const ids = new Set(openDecisions().map(d => d.supplier_id));
  ((S.plan || {}).lanes || []).filter(l => l.status === "critical" || l.gap > 0)
    .forEach(l => (l.bars || []).forEach(b => ids.add(b.supplier_id)));
  ids.delete(undefined);
  return ids;
}
function timeline() {
  const [a, b] = range(), X = t => ((ms(t) - a) / (b - a) * 100).toFixed(2);
  const ids = [], decs = S.decisions || [];
  (S.events || []).forEach(e => { if (e.supplier_id && !ids.includes(e.supplier_id)) ids.push(e.supplier_id); });
  openDecisions().forEach(d => { if (d.supplier_id && !ids.includes(d.supplier_id)) ids.push(d.supplier_id); });
  const crit = criticalSuppliers(), axisT = ticks(a, b);
  let html = axisT.map(t => `<div class="grid" style="left:${X(t)}%"></div>`).join("");
  html += `<div class="pastbg" style="width:${X(S.clock.now)}%"></div>`;
  const W = $("#lanes").clientWidth || 1000;
  ids.forEach(id => {
    const mine = decs.filter(d => d.supplier_id === id);
    const items = (S.events || []).filter(e => e.supplier_id === id && EV[e.type] && !(e.type === "decision" && mine.length)).map(e => {
      let [cls, icon] = EV[e.type], x = +X(e.at);
      if (x > 96) { x = 96; cls = "muted end"; }               // after the shown range: pinned at the lane's end
      if (!cls && e.severity === "risk") cls = "bad";
      let w = Math.min(260, 44 + String(e.title).length * 7.5);
      const from = x * W / 100 + (cls === "muted end" ? 11 - w : -11);
      if (cls !== "muted end") w = Math.max(Math.min(w, MIN_CHIP), Math.min(w, W - from));   // stays inside the lane
      return {e, cls, icon, x, w, from, to: from + w};
    });
    mine.forEach(d => {   // the decision window: open until the deadline, a decided one until it was decided
      const x = +X(d.at), until = d.status === "open" ? X(S.launch.deadline) : d.decided_at ? X(d.decided_at) : x;
      const pct = Math.max(2, until - x, 84 / W * 100), from = x * W / 100;   // wide enough for its flag
      items.push({win: true, done: d.status !== "open", x, pct, from, to: from + pct * W / 100});
    });
    const rows = placeRows(items);
    const chips = items.map(it => {
      const top = rows < 2 ? "" : rows === 2 ? `top:${it.row ? "74%" : "28%"}` : `top:${((it.row + .5) / rows * 100).toFixed(1)}%`;
      if (it.win) return `<div class="win${it.done ? " done" : ""}" style="left:${it.x}%;width:${it.pct}%;${top}"><span class="flag">${ic("gavel")}Decide</span></div>`;
      return `<button class="ev ${it.cls}" style="left:${it.x}%;${top};max-width:${Math.round(it.w)}px" title="${esc(it.e.title)}" data-ev="${it.e.id}"><span class="pin">${ic(it.icon)}</span><span class="lbl" style="overflow:hidden;text-overflow:ellipsis">${esc(it.e.title)}</span></button>`;
    }).join("");
    const cls = (rows > 1 ? " tall" : "") + (crit.size && !crit.has(id) ? " dim" : "");
    html += `<div class="lane${cls}"${rows > 2 ? ` style="height:${rows * 40}px"` : ""}><button class="lname"><b>${esc(supName(id))}</b></button>${chips}</div>`;
  });
  if (!ids.length) html += `<div class="lane"><span class="lname subtle">No supplier events yet</span></div>`;
  html += `<div class="after" style="left:${X(S.launch.deadline)}%"></div><div class="vline dead" style="left:${X(S.launch.deadline)}%"><span class="vlab" style="color:var(--redtext)">Deadline ${hm(S.launch.deadline)}</span></div><div class="now" style="left:${X(S.clock.now)}%"><span>Now</span></div>`;
  $("#lanes").innerHTML = html;
  $("#axis").innerHTML = axisT.map(t => `<span class="tick" style="left:${X(t)}%">${dhm(t)}</span>`).join("");
  document.querySelectorAll("[data-ev]").forEach(el => el.onclick = () => openEvent(+el.dataset.ev));
}
function feed() {
  const now = ms(S.clock.now);
  const items = (S.events || []).filter(e => ms(e.at) <= now).sort((x, y) => ms(y.at) - ms(x.at));
  $("#feed").innerHTML = items.length ? items.map(e => {
    const i = FEED_IC[e.type] || "info", k = e.severity === "risk" ? "k-bad" : e.severity === "ok" ? "k-good" : "";
    return `<li class="${k}" data-ev="${e.id}"><span class="ft">${when(e)}</span><span class="fn">${ic(i)}</span><span class="fx">${esc(e.title)}</span></li>`;
  }).join("") : `<li><span class="fx subtle">Nothing yet. Set the goal to start.</span></li>`;
  document.querySelectorAll("#feed [data-ev]").forEach(el => el.onclick = () => openEvent(+el.dataset.ev));
}

// ---------- decisions ----------
// The signer is the card route's answer, as in decision_card.js: the one on the record is only a proposal.
const SIG = new Map();
let sigAsk = 0;
const signerOf = d => SIG.get(d.decision_id) || {wait: true};
const canSign = s => !!(s && s.name && s.status !== "needs_human");
function note(m) {   // inline, never a browser dialog
  const n = $("#gpNote"); if (!n) return;
  n.textContent = m || ""; n.classList.toggle("on", !!m);
}
function signerText(s) {
  if (s.wait) return "Loading the signer…";
  if (s.failed) return "Signer unavailable";
  if (s.status === "needs_human") return "needs_human: a person has to confirm who signs";
  return s.name;
}
async function routedSigner(id) {
  const r = await fetch(`/api/lookahead/decisions/${encodeURIComponent(id)}`);
  if (!r.ok) throw new Error("no signer");
  const card = await r.json(), s = card.signer || {};
  const status = s.name ? ((card.approval || {}).status || s.status) : "needs_human";
  return {...s, status, name: status === "needs_human" ? "" : s.name};   // an unresolved route names nobody
}
async function lookupSigners() {
  const ask = ++sigAsk;
  const found = await Promise.all(openDecisions().map(d =>
    routedSigner(d.decision_id).catch(() => ({failed: true})).then(s => [d.decision_id, s])));
  if (ask !== sigAsk) return;   // a newer state asked again
  SIG.clear(); found.forEach(([id, s]) => SIG.set(id, s));
  if (found.length) render();
}
function signerLine(d) {
  const s = signerOf(d);
  return `<div class="dc-sign"><span class="act-av${s.name ? "" : " empty"}">${esc(s.name ? initials(s.name) : "?")}</span><span><b>${esc(signerText(s))}</b> · ${esc(s.role || "")}</span></div>`;
}
function acts(d) {
  const s = signerOf(d);
  const label = s.wait || s.failed || s.status === "needs_human" ? signerText(s) : s.name ? `Accept as ${s.name}` : "No signer can accept";
  return `<div class="dq-btns2"><button class="btn btn-ghost" data-rej="${esc(d.decision_id)}">Reject</button><button class="btn btn-primary" data-act="${esc(d.decision_id)}"${canSign(s) ? "" : " disabled"}>${ic("check")}${esc(label)}</button></div>`;
}
function dqCard(d) {
  const plan = (d.on_accept || []).map(t => `<li>${ic("send")}<span>${esc(t)}</span></li>`).join("");
  return `<article class="dq-card" id="dq-${esc(d.decision_id)}"><div class="dc-meta">${B("r", "Open")}<span class="dc-due">${ic("clock")}Raised ${esc(dhm(d.at))}</span></div>
  <h4 class="dc-t">${esc(d.title)}</h4><p class="dc-cause">${esc(d.recommendation || "")}</p>
  <div class="dc-stats"><div${srcTip(d.cost && d.cost.sources)}><b>${eur(d.cost && d.cost.eur)}</b><span>To pay</span></div><div${srcTip(d.impact && d.impact.sources)}><b class="bad">${eur(d.impact && d.impact.eur)}</b><span>Refunds if ignored</span></div></div>
  <div class="dc-plan"><div class="dc-plan-h"><span>On accept · ${esc(supName(d.supplier_id))}</span><span class="dc-more">${ic("arrow-right")}</span></div><ul>${plan}</ul></div>
  <div class="dc-foot">${signerLine(d)}${acts(d)}</div></article>`;
}
function why(d) {
  const ev = (S.events || []).filter(e => e.lane === d.lane && e.detail && (e.detail.reasoning || []).length);
  const rows = ev.flatMap(e => e.detail.reasoning).slice(-6);
  return rows.map(whyRow).join("");
}
function decCard(d) {
  const proms = (S.promises || []).filter(p => p.lane === d.lane);
  const steps = proms.map(p => {
    const k = p.state === "proven" ? "done" : p.state === "broken" ? "bad" : "";
    return `<li class="k-${k}"><span class="pd-t">${esc(dhm(p.due || p.next_check))}</span><i>${k === "done" ? ic("check") : k === "bad" ? ic("x") : ""}</i><span class="pd-x">${esc(supName(p.supplier_id))}: ${esc(p.what)}</span></li>`;
  }).join("") + `<li class="k-dead"><span class="pd-t">${esc(dhm(S.launch.deadline))}</span><i></i><span class="pd-x">Deadline</span></li>`;
  return `<article class="dc2"><div class="dc2-meta">${B("r", "Open")}<span class="dc2-due">${ic("clock")}<b>Raised ${esc(dhm(d.at))}</b></span></div>
  <h3 class="dc2-t">${esc(d.title)}</h3><p class="dc2-sum">${esc(d.recommendation || "")}</p>
  <div class="dc2-kpi"><div><span>To pay, on delivery</span><b>${eur(d.cost && d.cost.eur)}</b><small>${esc(d.cost && d.cost.text)}</small>${srcLine(d.cost && d.cost.sources)}</div>
   <div><span>Refunds if ignored</span><b class="bad">${eur(d.impact && d.impact.eur)}</b><small>${esc(d.impact && d.impact.text)}</small>${srcLine(d.impact && d.impact.sources)}</div>
   <div><span>Copies protected</span><b>${num(d.impact && d.impact.units)}</b><small>${esc((S.launch || {}).name || "")}</small>${srcLine(d.impact && d.impact.sources)}</div></div>
  <div class="dc2-cols"><section><p class="pd-h">${esc(supName(d.supplier_id))}</p><ol class="pd-steps">${steps}</ol></section>
   <section><p class="pd-h">${ic("sparkles")}Why the agent suggests this</p><dl class="pd-why">${why(d)}</dl></section></div>
  <div class="dc2-foot"><div class="pd-sign"><span class="act-av${signerOf(d).name ? "" : " empty"}">${esc(signerOf(d).name ? initials(signerOf(d).name) : "?")}</span><span><b>${esc(signerText(signerOf(d)))}</b><small>${esc([signerOf(d).rule, signerOf(d).reason].filter(Boolean).join(". "))}</small></span></div>${acts(d)}</div></article>`;
}
function decisions() {
  const open = openDecisions();
  $("#decCount").textContent = open.length; $("#dqCount").textContent = open.length;
  $("#dq").innerHTML = open.length ? dqCard(open[0]) : `<article class="dq-card ac"><p class="sm muted">Nothing waiting for you.</p></article>`;
  $("#decOpen").innerHTML = open.length ? open.map(decCard).join("") : `<p class="sm muted">Nothing waiting for you.</p>`;
  const done = (S.decisions || []).filter(d => d.status !== "open");
  $("#decDone").textContent = done.length;
  $("#decClosed").innerHTML = done.length ? done.map(d => `<tr><td style="font-weight:600">${esc(d.title)}<p class="xs subtle" style="font-weight:400">${esc(d.recommendation || "")}</p></td>
    <td><span class="who"><span class="who-av">${esc(initials(d.decided_by))}</span>${esc(d.decided_by || "")}</span></td>
    <td class="r"${srcTip(d.impact && d.impact.sources)}>${num(d.impact && d.impact.units)} copies</td><td class="r"${srcTip(d.cost && d.cost.sources)}>${eur(d.cost && d.cost.eur)}</td><td>${esc(dhm(d.decided_at || d.at))}</td>
    <td>${d.status === "accepted" ? B("g", "Approved") : B("r", d.status)}</td></tr>`).join("") : `<tr><td colspan="6" class="subtle">No decisions yet.</td></tr>`;
  document.querySelectorAll("#dq .dq-card[id]").forEach(c => c.onclick = e => { if (!e.target.closest("button")) show("decisions"); });
  document.querySelectorAll("[data-act]").forEach(b => b.onclick = e => { e.stopPropagation(); accept(b.dataset.act); });
  document.querySelectorAll("[data-rej]").forEach(b => b.onclick = e => { e.stopPropagation(); openRej(b.dataset.rej); });
}
async function accept(id) {
  const shown = SIG.get(id);
  if (!canSign(shown) || shown.busy) return;
  shown.busy = true;
  let fresh;
  try { fresh = await routedSigner(id); } catch (e) {
    shown.busy = false;
    note("Could not confirm the signer. Nothing was sent; try again.");
    return;
  }
  if (fresh.name !== shown.name || fresh.status !== shown.status) {
    SIG.set(id, fresh); render();
    note("The signer changed. Nothing was sent; check the name and click again.");
    return;
  }
  const approver = shown.name;
  const r = await fetch(`/api/lookahead/decisions/${encodeURIComponent(id)}/accept`, {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({approver})});
  shown.busy = false;
  note(r.ok ? "" : (await r.json().catch(() => ({}))).error || "Accept failed");
  load();
}
let REJ = null;
async function rejectDecision() {
  const approver = $("#rejBy").value.trim(), reason = $("#rejWhy").value.trim(), yes = $("#rejYes");
  if (!REJ || yes.disabled) return;
  if (!approver || !reason) return note("A rejection needs your name and a reason.");
  yes.disabled = true;
  try {
    const r = await fetch(`/api/lookahead/decisions/${encodeURIComponent(REJ)}/reject`, {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({approver, reason})});
    if (!r.ok) return note((await r.json().catch(() => ({}))).error || "Reject failed");
    note(""); closeAll(); load();
  } catch (e) {
    note("Reject failed: the screen could not reach the server.");
  } finally { yes.disabled = false; }
}
function openRej(id) {
  REJ = id; note(""); $("#rejBy").value = ""; $("#rejWhy").value = "";
  const d = (S.decisions || []).find(x => x.decision_id === id);
  $("#rejTitle").textContent = d ? d.title : id; $("#rejModal").classList.add("on");
}

// ---------- risks and monitoring ----------
function risks() {
  const plan = S.plan || {}, lanes = plan.lanes || [], cov = plan.coverage || {};
  const crit = lanes.filter(l => l.gap > 0 || l.status === "covered");
  const show = crit.length ? crit : lanes.slice(0, 3);
  const sups = (S.suppliers || []).filter(s => s.score && s.score.n > 0 && !s.scenario)
    .sort((x, y) => x.score.value - y.score.value);
  $("#gSup").innerHTML = sups.map(s => `<div class="gnode"${srcTip(s.score.sources)}><div class="gn-t"><b>${esc(s.name)}</b><span>${esc((s.parts || [])[0] || "")}</span></div><div class="gn-m"><b class="${tone(s.score.value)}">${Math.round(s.score.value * 100)}%</b><span>Reliability</span></div></div>`).join("");
  $("#gPart").innerHTML = show.map(l => `<div class="gnode ${l.gap > 0 ? "crit" : ""}"${srcTip(l.sources)}><div class="gn-t"><b>${esc(l.name)}</b><span>${esc(l.edition)} · ${esc(l.status)}</span></div><div class="gn-m"><b class="${l.gap > 0 ? "tone-r" : "tone-g"}">${l.gap > 0 ? "−" + num(l.gap) : "0"}</b><span>Gap</span></div></div>`).join("") || `<p class="sm subtle">Set the goal to rank the parts.</p>`;
  const ok = cov.needed && cov.covered >= cov.needed, pct = cov.needed ? Math.round(cov.covered / cov.needed * 100) : 0;
  $("#gProd").innerHTML = cov.needed ? `<div class="gnode prod ${ok ? "" : "crit"}"${srcTip(cov.sources)}><div class="gn-t"><b>${esc((S.launch || {}).name)}</b><span>${num(cov.covered)} of ${num(cov.needed)} covered</span></div><div class="gbar ${ok ? "ok" : ""}"><i style="width:${pct}%"></i></div></div>` : "";
  $("#weakNote").textContent = sups.length ? "Reliability from the order history in kb" : "";
  $("#coverText").textContent = cov.needed ? `${(S.launch || {}).name}: ${num(cov.covered)} of ${num(cov.needed)} covered` : "";
  $("#coverText").title = cov.needed ? srcText(cov.sources) : "";
  const P = S.promises || [];
  const st = p => p.state === "proven" ? '<span class="badge b-g">Kept</span>' : p.state === "broken" ? '<span class="badge b-n">Broken</span>' : '<span class="badge b-a">Watching</span>';
  $("#monBody").innerHTML = P.length ? P.map(p => `<tr><td style="font-weight:600">${esc(supName(p.supplier_id))}</td><td${srcTip(p.sources)}>${esc(p.what)}</td><td class="mono" style="font-size:13px">${p.state === "waiting" ? esc(dhm(p.next_check || p.due)) : "—"}</td><td>${st(p)}</td><td class="sm muted">${esc(p.fallback || "")}</td></tr>`).join("") : '<tr><td colspan="5" class="subtle">No promises yet.</td></tr>';
  $("#monBadge").textContent = "Watching " + P.filter(p => p.state === "waiting").length;
}

// ---------- goal, drawer, header ----------
function goal() {
  const qs = (S.launch || {}).goal_questions || [];
  $("#goalCard").hidden = !!S.goal || !qs.length;
  $("#goalWhy").textContent = qs.length ? qs[0].why : "";
  $("#goalList").innerHTML = qs.map(q => `<div><dt>${esc(q.q)}</dt><dd><b>${esc(q.suggested)}${q.unit ? " " + esc(q.unit) : ""}</b> · ${esc(q.why)}${srcLine(q.sources || [{label: `launch record, goal question ${q.id}${S.launch.scenario ? " (scenario input)" : ""}`}])}</dd></div>`).join("");
}
async function setGoal() {
  const answers = Object.fromEntries(((S.launch || {}).goal_questions || []).map(q => [q.id, q.suggested]));
  const r = await fetch("/api/lookahead/goal", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({answers})});
  note(r.ok ? "" : (await r.json().catch(() => ({}))).error || "Setting the goal failed");
  load();
}
function openEvent(id) {
  const e = (S.events || []).find(x => x.id === id); if (!e) return;
  const d = e.detail || {};
  $("#dTitle").innerHTML = `<p class="h-sm">${esc(e.title)}</p><p class="xs subtle">${esc(when(e))} · ${esc(e.type)}</p>`;
  $("#dBody").innerHTML = `${d.happened ? `<p>${esc(d.happened)}</p>` : ""}<dl class="pd-why">${(d.reasoning || []).map(whyRow).join("")}</dl>${d.sources ? srcLine(d.sources) : ""}${d.source ? `<p class="xs subtle">Source: ${esc([d.source.label, d.source.issue_key].filter(Boolean).join(", "))}</p>` : ""}${d.autonomy && d.autonomy.text ? `<p class="xs subtle">${esc(d.autonomy.text)}</p>` : ""}`;
  $("#drawer").classList.add("on"); $("#scrim").classList.add("on");
}
function closeAll() { ["#drawer", "#scrim", "#rejModal"].forEach(s => $(s).classList.remove("on")); }
function header() {
  $("#deadlineBadge").textContent = S.launch ? "Deadline " + dhm(S.launch.deadline) : "";
  $("#deadlineBadge").title = S.launch ? srcText([{label: `launch record${S.launch.scenario ? " (scenario input)" : ""}`}]) : "";
  $("#clockBadge").textContent = S.clock ? "Now " + dhm(S.clock.now) : "";
  const open = openDecisions()[0], last = (S.decisions || [])[0];
  const s = open ? signerOf(open) : {name: last && last.decided_by};
  $("#meBtn").textContent = initials(s.name); $("#meBtn").title = open ? signerText(s) : s.name || "";
}
function show(v) {
  VIEW = v;
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("on", t.dataset.view === v));
  document.querySelectorAll(".view").forEach(x => x.classList.toggle("on", x.id === "v-" + v));
  history.replaceState(null, "", "#v=" + v);
  if (v === "plan" && typeof pfWires === "function") requestAnimationFrame(pfWires);   // connectors need the view visible
}
function render() {
  if (!S || !S.launch) return;
  TZ = S.launch.timezone || TZ;
  header(); goal(); timeline(); feed(); decisions(); risks();
  if (typeof planView === "function") planView(S);
  hydrate();
}
async function load() {
  S = await (await fetch("/api/lookahead/state")).json();
  render();
  lookupSigners();
}
document.querySelectorAll(".tab").forEach(t => t.onclick = () => show(t.dataset.view));
document.querySelectorAll("[data-goview]").forEach(b => b.onclick = () => show(b.dataset.goview));
$("#goalSet").onclick = setGoal; $("#dClose").onclick = closeAll; $("#scrim").onclick = closeAll; $("#rejNo").onclick = closeAll; $("#rejYes").onclick = rejectDecision;
show((location.hash.match(/v=(\w+)/) || [])[1] || "timeline");
hydrate(); load();
let pending = null;
const es = new EventSource("/api/lookahead/stream");
const refresh = () => { clearTimeout(pending); pending = setTimeout(load, 150); };
es.addEventListener("record", refresh);   // the stream names its messages "record"; a reset arrives as one too
es.onmessage = refresh;
