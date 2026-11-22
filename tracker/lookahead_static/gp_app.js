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
const initials = n => String(n || "?").split(/\s+/).filter(Boolean).map(w => w[0]).slice(0, 2).join("").toUpperCase();
const tone = v => v >= 0.75 ? "tone-g" : v >= 0.55 ? "tone-am" : "tone-r";

let S = null, VIEW = "timeline";
const EV = {  // record type -> chip class and icon, as in the prototype
  offer_in: ["", "inbox"], agent: ["agent", "sparkles"], email_out: ["agent", "send"], email_in: ["good", "mail"],
  check_in: ["good", "check"], cancellation: ["bad", "x"], decision: ["agent", "gavel"], document: ["", "document"],
  delay: ["", "truck"],
};
const FEED_IC = {cancellation: "alert-triangle", email_in: "mail", email_out: "send", check_in: "check", goal: "check",
  agent: "sparkles", decision: "gavel", offer_in: "inbox"};

function supName(id) { const s = (S.suppliers || []).find(x => x.supplier_id === id); return s ? s.name : id; }
function openDecisions() { return (S.decisions || []).filter(d => d.status === "open"); }

// ---------- timeline ----------
function range() {
  const end = ms(S.launch.deadline) + 3600e3;
  const ats = (S.events || []).map(e => ms(e.at)).concat([ms(S.clock.now)]);
  const start = Math.min(end - 26 * 3600e3, Math.min(...ats) - 1800e3);
  return [start, end];
}
function timeline() {
  const [a, b] = range(), X = t => ((ms(t) - a) / (b - a) * 100).toFixed(2);
  const ids = [];
  (S.events || []).forEach(e => { if (e.supplier_id && !ids.includes(e.supplier_id)) ids.push(e.supplier_id); });
  openDecisions().forEach(d => { if (d.supplier_id && !ids.includes(d.supplier_id)) ids.push(d.supplier_id); });
  let html = "";
  const step = 4 * 3600e3, first = Math.ceil(a / step) * step;
  for (let t = first; t < b; t += step) html += `<div class="grid" style="left:${X(t)}%"></div>`;
  html += `<div class="pastbg" style="width:${X(S.clock.now)}%"></div>`;
  ids.forEach(id => {
    let chips = "", last = {};
    (S.events || []).filter(e => e.supplier_id === id && EV[e.type]).forEach((e, i) => {
      const [cls, icon] = EV[e.type], x = +X(e.at);
      const top = last.x != null && Math.abs(x - last.x) < 9 && last.top !== "74%" ? "74%" : "28%";
      last = {x, top};
      chips += `<button class="ev ${cls}" style="left:${x}%;top:${top}" data-ev="${e.id}"><span class="pin">${ic(icon)}</span><span class="lbl">${esc(e.title)}</span></button>`;
    });
    openDecisions().filter(d => d.supplier_id === id).forEach(d => {
      chips += `<div class="win" style="left:${X(d.at)}%;width:${Math.max(2, X(S.launch.deadline) - X(d.at))}%"><span class="flag">${ic("gavel")}Decide</span></div>`;
    });
    html += `<div class="lane tall"><button class="lname"><b>${esc(supName(id))}</b></button>${chips}</div>`;
  });
  if (!ids.length) html += `<div class="lane"><span class="lname subtle">No supplier events yet</span></div>`;
  html += `<div class="after" style="left:${X(S.launch.deadline)}%"></div><div class="vline dead" style="left:${X(S.launch.deadline)}%"><span class="vlab" style="color:var(--redtext)">Deadline ${hm(S.launch.deadline)}</span></div><div class="now" style="left:${X(S.clock.now)}%"><span>Now</span></div>`;
  $("#lanes").innerHTML = html;
  let ticks = "";
  for (let t = first; t < b; t += step) ticks += `<span class="tick" style="left:${X(t)}%">${dhm(t)}</span>`;
  $("#axis").innerHTML = ticks;
  document.querySelectorAll("[data-ev]").forEach(el => el.onclick = () => openEvent(+el.dataset.ev));
}
function feed() {
  const items = [...(S.events || [])].sort((x, y) => ms(y.at) - ms(x.at));
  $("#feed").innerHTML = items.length ? items.map(e => {
    const i = FEED_IC[e.type] || "info", k = e.severity === "risk" ? "k-bad" : e.severity === "ok" ? "k-good" : "";
    return `<li class="${k}" data-ev="${e.id}"><span class="ft">${dhm(e.at)}</span><span class="fn">${ic(i)}</span><span class="fx">${esc(e.title)}</span></li>`;
  }).join("") : `<li><span class="fx subtle">Nothing yet. Set the goal to start.</span></li>`;
  document.querySelectorAll("#feed [data-ev]").forEach(el => el.onclick = () => openEvent(+el.dataset.ev));
}

// ---------- decisions ----------
function signerLine(d) {
  const s = d.signer || {};
  return `<div class="dc-sign"><span class="act-av${s.name ? "" : " empty"}">${esc(initials(s.name))}</span><span><b>${esc(s.name || s.role || "Signer unresolved")}</b> · ${esc(s.role || "")}</span></div>`;
}
function acts(d) {
  return `<div class="dq-btns2"><button class="btn btn-ghost" data-rej="${esc(d.decision_id)}">Reject</button><button class="btn btn-primary" data-act="${esc(d.decision_id)}">${ic("check")}Accept and confirm</button></div>`;
}
function dqCard(d) {
  const plan = (d.on_accept || []).map(t => `<li>${ic("send")}<span>${esc(t)}</span></li>`).join("");
  return `<article class="dq-card"><div class="dc-meta">${B("r", "Urgent")}<span class="dc-due">${ic("clock")}${esc(dhm(d.at))}</span></div>
  <h4 class="dc-t">${esc(d.title)}</h4><p class="dc-cause">${esc(d.recommendation || "")}</p>
  <div class="dc-stats"><div><b>${eur(d.cost && d.cost.eur)}</b><span>To pay</span></div><div><b class="bad">${eur(d.impact && d.impact.eur)}</b><span>Refunds if ignored</span></div></div>
  <div class="dc-plan"><div class="dc-plan-h"><span>On accept</span></div><ul>${plan}</ul></div>
  <div class="dc-foot">${signerLine(d)}${acts(d)}</div></article>`;
}
function why(d) {
  const ev = (S.events || []).filter(e => e.lane === d.lane && e.detail && (e.detail.reasoning || []).length);
  const rows = ev.flatMap(e => e.detail.reasoning).slice(-6);
  return rows.map(r => `<div><dt>${esc(r.text)}</dt><dd>${esc(r.value || "")}</dd></div>`).join("");
}
function decCard(d) {
  const proms = (S.promises || []).filter(p => p.lane === d.lane);
  const steps = proms.map(p => {
    const k = p.state === "proven" ? "done" : p.state === "broken" ? "bad" : "";
    return `<li class="k-${k}"><span class="pd-t">${esc(dhm(p.due || p.next_check))}</span><i>${k === "done" ? ic("check") : k === "bad" ? ic("x") : ""}</i><span class="pd-x">${esc(supName(p.supplier_id))}: ${esc(p.what)}</span></li>`;
  }).join("") + `<li class="k-dead"><span class="pd-t">${esc(dhm(S.launch.deadline))}</span><i></i><span class="pd-x">Deadline</span></li>`;
  return `<article class="dc2"><div class="dc2-meta">${B("r", "Urgent")}<span class="dc2-kind">Backup</span><span class="dc2-due">${ic("clock")}<b>${esc(dhm(d.at))}</b></span></div>
  <h3 class="dc2-t">${esc(d.title)}</h3><p class="dc2-sum">${esc(d.recommendation || "")}</p>
  <div class="dc2-kpi"><div><span>To pay, on delivery</span><b>${eur(d.cost && d.cost.eur)}</b><small>${esc(d.cost && d.cost.text)}</small></div>
   <div><span>Refunds if ignored</span><b class="bad">${eur(d.impact && d.impact.eur)}</b><small>${esc(d.impact && d.impact.text)}</small></div>
   <div><span>Copies protected</span><b>${num(d.impact && d.impact.units)}</b><small>${esc((S.launch || {}).name || "")}</small></div></div>
  <div class="dc2-cols"><section><p class="pd-h">${esc(supName(d.supplier_id))}</p><ol class="pd-steps">${steps}</ol></section>
   <section><p class="pd-h">${ic("sparkles")}Why the agent suggests this</p><dl class="pd-why">${why(d)}</dl></section></div>
  <div class="dc2-foot"><div class="pd-sign"><span class="act-av${(d.signer || {}).name ? "" : " empty"}">${esc(initials((d.signer || {}).name))}</span><span><b>${esc((d.signer || {}).name || "Signer unresolved")}</b><small>${esc((d.signer || {}).rule || "")}</small></span></div>${acts(d)}</div></article>`;
}
function decisions() {
  const open = openDecisions();
  $("#decCount").textContent = open.length; $("#dqCount").textContent = open.length;
  $("#dq").innerHTML = open.length ? dqCard(open[0]) : `<article class="dq-card ac"><p class="sm muted">Nothing waiting for you.</p></article>`;
  $("#decOpen").innerHTML = open.length ? open.map(decCard).join("") : `<p class="sm muted">Nothing waiting for you.</p>`;
  const done = (S.decisions || []).filter(d => d.status !== "open");
  $("#decDone").textContent = done.length;
  $("#decClosed").innerHTML = done.length ? done.map(d => `<tr><td style="font-weight:600">${esc(d.title)}<p class="xs subtle" style="font-weight:400">${esc(d.recommendation || "")}</p></td>
    <td><span class="who"><span class="who-av">${esc(initials(d.by || (d.signer || {}).name))}</span>${esc(d.by || (d.signer || {}).name || "")}</span></td>
    <td class="r">${num(d.impact && d.impact.units)} copies</td><td class="r">${eur(d.cost && d.cost.eur)}</td><td>${esc(dhm(d.decided_at || d.at))}</td>
    <td>${d.status === "accepted" ? B("g", "Approved") : B("r", d.status)}</td></tr>`).join("") : `<tr><td colspan="6" class="subtle">No decisions yet.</td></tr>`;
  document.querySelectorAll("[data-act]").forEach(b => b.onclick = e => { e.stopPropagation(); accept(b.dataset.act); });
  document.querySelectorAll("[data-rej]").forEach(b => b.onclick = e => { e.stopPropagation(); openRej(b.dataset.rej); });
}
async function accept(id) {
  const d = (S.decisions || []).find(x => x.decision_id === id);
  const approver = (d && d.signer && d.signer.name) || prompt("Who signs this decision?");
  if (!approver) return;
  const r = await fetch(`/api/lookahead/decisions/${encodeURIComponent(id)}/accept`, {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({approver})});
  if (!r.ok) alert((await r.json().catch(() => ({}))).error || "Accept failed");
  load();
}
function openRej(id) {
  const d = (S.decisions || []).find(x => x.decision_id === id);
  $("#rejTitle").textContent = d ? d.title : id; $("#rejModal").classList.add("on");
}

// ---------- risks and monitoring ----------
function risks() {
  const plan = S.plan || {}, lanes = plan.lanes || [], cov = plan.coverage || {};
  const crit = lanes.filter(l => l.gap > 0 || l.status === "covered");
  const show = crit.length ? crit : lanes.slice(0, 3);
  const sups = (S.suppliers || []).filter(s => s.score && s.score.n > 0 && !s.supplier_id.startsWith("SCN"))
    .sort((x, y) => x.score.value - y.score.value);
  $("#gSup").innerHTML = sups.map(s => `<div class="gnode"><div class="gn-t"><b>${esc(s.name)}</b><span>${esc((s.parts || [])[0] || "")}</span></div><div class="gn-m"><b class="${tone(s.score.value)}">${Math.round(s.score.value * 100)}%</b><span>Reliability</span></div></div>`).join("");
  $("#gPart").innerHTML = show.map(l => `<div class="gnode ${l.gap > 0 ? "crit" : ""}"><div class="gn-t"><b>${esc(l.name)}</b><span>${esc(l.edition)} · ${esc(l.status)}</span></div><div class="gn-m"><b class="${l.gap > 0 ? "tone-r" : "tone-g"}">${l.gap > 0 ? "−" + num(l.gap) : "0"}</b><span>Gap</span></div></div>`).join("") || `<p class="sm subtle">Set the goal to rank the parts.</p>`;
  const ok = cov.needed && cov.covered >= cov.needed, pct = cov.needed ? Math.round(cov.covered / cov.needed * 100) : 0;
  $("#gProd").innerHTML = cov.needed ? `<div class="gnode prod ${ok ? "" : "crit"}"><div class="gn-t"><b>${esc((S.launch || {}).name)}</b><span>${num(cov.covered)} of ${num(cov.needed)} covered</span></div><div class="gbar ${ok ? "ok" : ""}"><i style="width:${pct}%"></i></div></div>` : "";
  $("#weakNote").textContent = sups.length ? "Reliability from the order history in kb" : "";
  $("#coverText").textContent = cov.needed ? `${(S.launch || {}).name}: ${num(cov.covered)} of ${num(cov.needed)} covered` : "";
  const P = S.promises || [];
  const st = p => p.state === "proven" ? '<span class="badge b-g">Kept</span>' : p.state === "broken" ? '<span class="badge b-n">Broken</span>' : '<span class="badge b-a">Watching</span>';
  $("#monBody").innerHTML = P.length ? P.map(p => `<tr><td style="font-weight:600">${esc(supName(p.supplier_id))}</td><td>${esc(p.what)}</td><td class="mono" style="font-size:13px">${p.state === "waiting" ? esc(dhm(p.next_check || p.due)) : "—"}</td><td>${st(p)}</td><td class="sm muted">${esc(p.fallback || "")}</td></tr>`).join("") : '<tr><td colspan="5" class="subtle">No promises yet.</td></tr>';
  $("#monBadge").textContent = "Watching " + P.filter(p => p.state === "waiting").length;
}

// ---------- goal, drawer, header ----------
function goal() {
  const qs = (S.launch || {}).goal_questions || [];
  $("#goalCard").hidden = !!S.goal || !qs.length;
  $("#goalWhy").textContent = qs.length ? qs[0].why : "";
  $("#goalList").innerHTML = qs.map(q => `<div><dt>${esc(q.q)}</dt><dd><b>${esc(q.suggested)}${q.unit ? " " + esc(q.unit) : ""}</b> · ${esc(q.why)}</dd></div>`).join("");
}
async function setGoal() {
  const answers = Object.fromEntries(((S.launch || {}).goal_questions || []).map(q => [q.id, q.suggested]));
  const r = await fetch("/api/lookahead/goal", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({answers})});
  if (!r.ok) alert((await r.json().catch(() => ({}))).error || "Setting the goal failed");
  load();
}
function openEvent(id) {
  const e = (S.events || []).find(x => x.id === id); if (!e) return;
  const d = e.detail || {};
  $("#dTitle").innerHTML = `<p class="h-sm">${esc(e.title)}</p><p class="xs subtle">${esc(dhm(e.at))} · ${esc(e.type)}</p>`;
  $("#dBody").innerHTML = `${d.happened ? `<p>${esc(d.happened)}</p>` : ""}<dl class="pd-why">${(d.reasoning || []).map(r => `<div><dt>${esc(r.text)}</dt><dd>${esc(r.value || "")}</dd></div>`).join("")}</dl>${d.autonomy && d.autonomy.text ? `<p class="xs subtle">${esc(d.autonomy.text)}</p>` : ""}`;
  $("#drawer").classList.add("on"); $("#scrim").classList.add("on");
}
function closeAll() { ["#drawer", "#scrim", "#rejModal"].forEach(s => $(s).classList.remove("on")); }
function header() {
  $("#deadlineBadge").textContent = S.launch ? "Deadline " + dhm(S.launch.deadline) : "";
  $("#clockBadge").textContent = S.clock ? "Now " + dhm(S.clock.now) : "";
  const s = (openDecisions()[0] || (S.decisions || [])[0] || {}).signer || {};
  $("#meBtn").textContent = initials(s.name); $("#meBtn").title = s.name ? `${s.name}, ${s.role}` : "";
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
}
document.querySelectorAll(".tab").forEach(t => t.onclick = () => show(t.dataset.view));
document.querySelectorAll("[data-goview]").forEach(b => b.onclick = () => show(b.dataset.goview));
$("#goalSet").onclick = setGoal; $("#dClose").onclick = closeAll; $("#scrim").onclick = closeAll; $("#rejNo").onclick = closeAll;
show((location.hash.match(/v=(\w+)/) || [])[1] || "timeline");
hydrate(); load();
let pending = null;
const es = new EventSource("/api/lookahead/stream");
const refresh = () => { clearTimeout(pending); pending = setTimeout(load, 150); };
es.addEventListener("record", refresh);   // the stream names its messages "record"; a reset arrives as one too
es.onmessage = refresh;
