// Grand Plan Lio "Email log" view: the prototype's two-pane layout (analysis/design/grand-plan-lio, #v=mail), filled
// from /api/lookahead/mail, which reads the mail store through MailTools' read methods only. Nothing here sends.
"use strict";
const MAIL = {T: null, items: [], available: true, sel: null, filter: "all", loaded: false};
const mailLang = () => {
  const q = new URLSearchParams(location.search).get("lang") || document.documentElement.lang || "en";
  return q.toLowerCase().startsWith("zh") ? "zh" : "en";
};
const mt = k => ((MAIL.T || {})[k]) ?? k;

function mailSupplier(id) {
  const s = ((typeof S !== "undefined" && S && S.suppliers) || []).find(x => x.supplier_id === id);
  return s ? s.name : id;
}
function mailWho(m) {   // contact and company, as the prototype's "Name · Company"
  const c = m.contact || {}, name = c.name || m.from_name || c.address || "";
  const company = m.supplier_id ? mailSupplier(m.supplier_id) : "";
  return [name, company].filter(Boolean).filter((v, i, a) => a.indexOf(v) === i).join(" · ");
}
function mailDecision(m) {   // the decision whose drafts are in this thread, else the latest one for the supplier
  const ds = (typeof S !== "undefined" && S && S.decisions) || [];
  const ids = new Set(m.draft_ids || []);
  return ds.find(d => (d.draft_ids || []).some(x => ids.has(String(x))))
    || (m.supplier_id ? ds.find(d => d.supplier_id === m.supplier_id) : null) || null;
}
function mailBody(m) {
  const text = mailLang() === "en" && m.body_en && m.lang && !String(m.lang).startsWith("en") ? m.body_en : m.body;
  return esc(text || "").replace(/\n/g, "<br>");
}
function mailTime(m) { return m.at ? dhm(m.at) : ""; }

function mailList() {
  const F = MAIL.items.filter(m => MAIL.filter === "all" || m.direction === MAIL.filter);
  if (!F.find(m => m.message_id === MAIL.sel)) MAIL.sel = F[0] ? F[0].message_id : null;
  const empty = MAIL.available ? mt("empty") : mt("unavailable");
  $("#mList").innerHTML = F.map(m => `<li><button class="mitem${m.message_id === MAIL.sel ? " on" : ""}" data-m="${esc(m.message_id)}"><span class="mdir ${m.direction}">${ic(m.direction === "out" ? "send" : "inbox")}</span><span class="mtx"><span class="mrow"><b>${esc(mailWho(m))}</b><time>${esc(mailTime(m))}</time></span><span class="msub">${esc(m.subject || mt("no_subject"))}</span></span></button></li>`).join("")
    || `<li class="dq-empty">${esc(empty)}</li>`;
  return F.find(m => m.message_id === MAIL.sel) || null;
}
function mailRead(m) {
  if (!m) { $("#mRead").innerHTML = ""; return; }
  const out = m.direction === "out", to = (m.to || []).join(", "), d = mailDecision(m);
  $("#mRead").innerHTML = `<div class="mr-h"><span class="badge ${out ? "b-a" : "b-n"}">${esc(out ? mt("sent_badge") : mt("received_badge"))}</span><h3 class="mr-s">${esc(m.subject || mt("no_subject"))}</h3>
  <dl class="mr-meta"><dt>${esc(mt("from"))}</dt><dd>${esc(m.from_name ? `${m.from_name} <${m.from || ""}>` : m.from || "")}</dd><dt>${esc(mt("to"))}</dt><dd>${esc(to)}</dd><dt>${esc(mt("time"))}</dt><dd>${esc(mailTime(m))}</dd></dl></div>
  <div class="mr-b">${mailBody(m)}</div>${(m.attachments || []).length ? `<div class="mr-att">${m.attachments.map(a => `<span>${ic("document")}${esc(a)}</span>`).join("")}</div>` : ""}
  ${d ? `<button class="btn btn-text mr-link" data-mdec="${esc(d.decision_id)}">${ic("sparkles")}${esc(mt("related"))}</button>` : ""}`;
  const l = $("#mRead .mr-link");
  if (l) l.onclick = () => show("decisions");
}
function mails() {
  if (!MAIL.T) return;
  mailRead(mailList());
  document.querySelectorAll(".mitem").forEach(b => b.onclick = () => { MAIL.sel = b.dataset.m; mails(); });
}
function mailLabels() {
  const tab = document.querySelector('.tab[data-view="mail"] .mtab');
  if (tab) tab.textContent = mt("tab");
  const h = $("#mTitle"); if (h) h.textContent = mt("title");
  document.querySelectorAll("[data-mf]").forEach(b => { b.textContent = mt({all: "all", out: "sent", in: "received"}[b.dataset.mf]); });
}
async function mailLoad() {
  try {
    if (!MAIL.T) MAIL.T = (await (await fetch("/api/lookahead/mail/strings")).json())[mailLang()] || {};
    mailLabels();
    const r = await (await fetch("/api/lookahead/mail")).json();
    MAIL.items = r.messages || []; MAIL.available = r.available !== false;
  } catch (e) {
    MAIL.items = []; MAIL.available = false; MAIL.T = MAIL.T || {};
  }
  MAIL.loaded = true;
  mails();
}
document.querySelectorAll("[data-mf]").forEach(b => b.onclick = () => {
  MAIL.filter = b.dataset.mf;
  document.querySelectorAll("[data-mf]").forEach(x => x.classList.toggle("on", x === b));
  mails();
});
document.querySelectorAll('.tab[data-view="mail"]').forEach(t => t.addEventListener("click", mailLoad));
mailLoad();
