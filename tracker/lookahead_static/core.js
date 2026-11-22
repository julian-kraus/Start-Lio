"use strict";
const SVGNS = "http://www.w3.org/2000/svg";
const params = new URLSearchParams(location.search);
const S = {
  state: null, lastId: 0, seen: null, newIds: new Set(), view: "overview", sel: null, range: "launch",
  hlLane: null, decisionOpen: null, dismissed: new Set(), prevNow: null, prevCover: null, prevLaneStatus: {},
  bannerKey: null, flashLanes: new Set(),
};

// ---------- small DOM helpers (all data goes in as text, never as HTML) ----------
function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) n.append(c instanceof Node ? c : String(c));
  return n;
}
function sv(tag, attrs, ...kids) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) n.addEventListener(k.slice(2), v); else n.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return n;
}
const $ = (id) => document.getElementById(id);
const tz = () => (S.state && S.state.launch && S.state.launch.timezone) || undefined;
const ms = (iso) => new Date(iso).getTime();
function fmt(iso, opts) {
  if (!iso) return "";
  return new Intl.DateTimeFormat("en-GB", { timeZone: tz(), weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false, ...opts }).format(new Date(iso));
}
const hm = (iso) => fmt(iso, { weekday: undefined });
const eur = (v) => "€" + Math.round(v).toLocaleString("en-US");
const num = (v) => Math.round(v).toLocaleString("en-US");
function scoreClass(v) { return v >= 0.75 ? "s-good" : v >= 0.6 ? "s-mid" : "s-bad"; }
function initials(name) { return (name || "?").split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase(); }
function toast(msg, err) {
  const t = el("div", { class: "toast" + (err ? " err" : "") }, msg);
  $("toast-root").replaceChildren(t);
  setTimeout(() => t.remove(), err ? 5000 : 3200);
}
async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

// ---------- data ----------
const lanes = () => (S.state.plan && S.state.plan.lanes) || [];
const supplier = (id) => S.state.suppliers.find((s) => s.supplier_id === id);
const openDecisions = () => S.state.decisions.filter((d) => d.status === "open");
const nowMs = () => ms(S.state.clock.now);

async function load() {
  let st;
  try { st = await api("/api/lookahead/state"); } catch (e) { toast("Cannot load state: " + e.message, true); return; }
  const first = S.state === null;
  const prev = S.state;
  S.prevNow = prev ? nowMs() : null;
  S.prevCover = prev && prev.plan ? prev.plan.coverage : null;
  S.state = st;
  S.newIds = new Set();
  if (!first) for (const e of st.events) if (e.id > S.lastId) S.newIds.add(e.id);
  if (!first && st.plan) for (const b of st.plan.lanes.flatMap((l) => l.bars || [])) if (!prev.plan || !prev.plan.lanes.some((l) => (l.bars || []).some((x) => x.id === b.id && x.kind === b.kind))) S.newIds.add("bar:" + b.id + b.kind);
  S.flashLanes = new Set();
  for (const l of lanes()) {
    if (!first && l.status === "critical" && S.prevLaneStatus[l.lane_id] !== "critical") S.flashLanes.add(l.lane_id);
    S.prevLaneStatus[l.lane_id] = l.status;
  }
  // a decision that just opened takes the stage: this is the moment a human decides
  const fresh = openDecisions().find((d) => !S.dismissed.has(d.decision_id) && (first || !prev.decisions.some((p) => p.decision_id === d.decision_id && p.status === "open")));
  // On the Overview the decision is already the first thing on the page; elsewhere it takes the stage.
  if (fresh && !first && S.view !== "overview") S.decisionOpen = fresh.decision_id;
  if (S.decisionOpen && !openDecisions().some((d) => d.decision_id === S.decisionOpen)) S.decisionOpen = null;
  S.lastId = st.last_id;
  S.clockLoaded = nowMs();
  render();
  // animations belong to the load that brought the change, not to later re-renders
  S.newIds = new Set(); S.flashLanes = new Set(); S.prevCover = null; S.prevNow = null;
}
let pending = null;
function scheduleLoad() { clearTimeout(pending); pending = setTimeout(load, 90); }
function connect() {
  const es = new EventSource("/api/lookahead/stream?after=" + (S.lastId || 0));
  es.addEventListener("record", (m) => {
    const rec = JSON.parse(m.data);
    if (rec.kind === "reset") { S.lastId = 0; S.state = null; S.dismissed.clear(); S.decisionOpen = null; S.sel = null; S.prevLaneStatus = {}; }
    scheduleLoad();
  });
  es.onerror = () => {};
}
