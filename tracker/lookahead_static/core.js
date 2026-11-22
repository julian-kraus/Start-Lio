"use strict";
const SVGNS = "http://www.w3.org/2000/svg";
const params = new URLSearchParams(location.search);
const S = {
  state: null, lastId: 0, seen: null, newIds: new Set(), view: "overview", sel: null, range: "launch",
  hlLane: null, decisionOpen: null, dismissed: new Set(), prevNow: null, prevCover: null, prevLaneStatus: {},
  bannerKey: null, flashLanes: new Set(), language: localStorage.getItem("lookahead-language") || "en", strings: {}, menu: false,
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
const num = (v) => (v == null || isNaN(v) ? "–" : Math.round(v).toLocaleString("en-US"));  // a missing number is never drawn as one
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

const icon=(name,large=false)=>{const paths={timeline:"M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 M12 7v5l3.5 2",plan:"M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z M14 2v6h6M9 13h6M9 17h6",weak:"m21.7 18-8-14a2 2 0 0 0-3.4 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3z M12 9v4M12 17h.01",decisions:"m9.5 5.5 3.5-3.5 6.5 6.5-3.5 3.5z M13 9l-8.5 8.5M3 19l1.5-1.5M12 21h9",mail:"M3 5h18v14H3z M3 7l9 6 9-6",clock:"M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16zm0 4v4l3 2",rules:"M5 4h14v16H5zM8 8h8M8 12h8M8 16h5",chevron:"m8 10 4 4 4-4"};const n=document.createElementNS("http://www.w3.org/2000/svg","svg");n.setAttribute("class","ic"+(large?" lg":""));n.setAttribute("viewBox","0 0 24 24");n.setAttribute("aria-hidden","true");const p=document.createElementNS("http://www.w3.org/2000/svg","path");p.setAttribute("d",paths[name]||paths.timeline);n.append(p);return n};
function textNode(value){return document.createTextNode(value==null?"":String(value))}
function button(label,handler,classes=""){const b=document.createElement("button");b.className=classes;b.append(textNode(label));if(handler)b.addEventListener("click",handler);return b}
function formatDeadline(value){if(!value)return "";const date=new Date(value);if(Number.isNaN(date.getTime()))return String(value);try{return new Intl.DateTimeFormat(S.language==="zh"?"zh-TW":"en-GB",{timeZone:tz(),weekday:"short",hour:"2-digit",minute:"2-digit",hour12:false}).format(date)}catch(_){return String(value)}}

function route(){const match=location.hash.match(/^#v=(timeline|plan|weak|decisions|mail|rules)$/);return match?match[1]:"timeline"}
function setRoute(view){const next="#v="+view;if(location.hash!==next)history.pushState({view},"",next);render()}
function setView(view){setRoute(view)}
window.addEventListener("hashchange",()=>render());window.addEventListener("popstate",()=>render());
