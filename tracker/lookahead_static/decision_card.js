"use strict";
// ---------- decision overlay ----------
function acceptButton(d, err, after) {
  const signer = d.signer || {};
  const b = el("button", { class: "btn primary big", onclick: async () => {
    b.disabled = true; err.textContent = "";
    try {
      const r = await api(`/api/lookahead/decisions/${encodeURIComponent(d.decision_id)}/accept`, { approver: signer.name || "" });
      S.decisionOpen = null;
      if (after) after();
      toast(r.sent && r.sent.length ? `Accepted. ${r.sent.length} email(s) sent.` : "Accepted. Actions are going out.");
      render();
    } catch (e) { err.textContent = e.message; b.disabled = false; }
  } }, signer.name ? `Accept as ${signer.name}` : "Accept");
  return b;
}
function renderOverlay() {
  const o = $("overlay"), main = $("main");
  const d = S.decisionOpen && S.state.decisions.find((x) => x.decision_id === S.decisionOpen && x.status === "open");
  if (!d) { o.hidden = true; main.classList.remove("stepped"); o.replaceChildren(); return; }
  main.classList.add("stepped");
  o.hidden = false;
  if (o.dataset.id === d.decision_id && o.firstChild) return;  // keep the card (and any in-flight click) stable
  o.dataset.id = d.decision_id;
  const err = el("div", { class: "err" });
  const signer = d.signer || {};
  const accept = acceptButton(d, err, () => { o.dataset.id = ""; });
  const opts = el("div", { class: "opts" , hidden: true }, (d.options || []).map((x) => el("div", { class: "alt" }, el("b", {}, x.label), el("span", {}, x.text))));
  o.replaceChildren(el("div", { class: "dcard", role: "dialog", "aria-modal": "true", "aria-label": d.title },
    el("div", { class: "kicker" }, "Decision needed"),
    el("h2", {}, d.title),
    el("div", { class: "rec" }, d.recommendation || ""),
    el("div", { class: "nums" },
      el("div", { class: "prot" }, el("b", {}, d.impact && d.impact.eur ? eur(d.impact.eur) : "–"), el("span", {}, d.impact ? d.impact.text || "protected" : "")),
      el("div", {}, el("b", {}, d.cost && d.cost.eur != null ? eur(d.cost.eur) : "–"), el("span", {}, d.cost ? d.cost.text || "cost" : ""))),
    el("div", { class: "signer" }, el("div", { class: "avatar" }, initials(signer.name)),
      el("div", {}, el("b", {}, signer.name ? `${signer.name}, ${signer.role}` : "You"), el("div", { class: "src" }, signer.rule || ""))),
    (d.on_accept || []).length ? el("ul", {}, d.on_accept.map((a) => el("li", {}, a))) : null,
    opts,
    el("div", { class: "acts" }, accept,
      (d.options || []).length ? el("button", { class: "btn", onclick: () => { opts.hidden = !opts.hidden; } }, "Other options") : null,
      el("button", { class: "btn", onclick: () => { S.dismissed.add(d.decision_id); S.decisionOpen = null; o.dataset.id = ""; render(); } }, "Later")),
    err));
  setTimeout(() => accept.focus(), 50);
}
