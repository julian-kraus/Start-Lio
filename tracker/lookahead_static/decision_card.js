"use strict";
// ---------- decision overlay ----------
// The signer comes from the server's routing on kb facts (card route); the one stored on the decision is only the
// producer's proposal, and Accept is refused for anyone but the routed signer.
async function routedSigner(d) {
  const card = await api(`/api/lookahead/decisions/${encodeURIComponent(d.decision_id)}`);
  return card.signer || {};
}
// Accept posts exactly the name the button shows. It stays disabled until the routed signer is loaded; a click
// checks the signer again and posts nothing when it changed or cannot be read, so nobody approves in a name the
// screen did not show.
function acceptButton(d, err, after, onSigner) {
  let shown = null;
  const show = (s) => {
    shown = s.name || null;
    b.textContent = shown ? `Accept as ${shown}` : "No signer can accept";
    b.disabled = !shown;
    if (onSigner) onSigner(s);
  };
  const b = el("button", { class: "btn primary big", disabled: true, onclick: async () => {
    if (!shown || b.disabled) return;
    const name = shown;
    b.disabled = true; err.textContent = "";
    let fresh;
    try { fresh = await routedSigner(d); } catch (e) {
      err.textContent = "Could not confirm the signer. Nothing was sent; try again.";
      b.disabled = false;
      return;
    }
    if (fresh.name !== name) {
      show(fresh);
      err.textContent = "The signer changed. Nothing was sent; check the name and click again.";
      return;
    }
    try {
      const r = await api(`/api/lookahead/decisions/${encodeURIComponent(d.decision_id)}/accept`, { approver: name });
      S.decisionOpen = null;
      if (after) after();
      toast(r.sent && r.sent.length ? `Accepted. ${r.sent.length} email(s) sent.` : "Accepted. Actions are going out.");
      render();
    } catch (e) { err.textContent = e.message; b.disabled = false; }
  } }, "Loading the signer…");
  routedSigner(d).then(show).catch(() => { b.textContent = "Signer unavailable"; });
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
  const who = el("b", {}, "Loading the signer…"), rule = el("div", { class: "src" }, ""), face = el("div", { class: "avatar" }, "");
  const accept = acceptButton(d, err, () => { o.dataset.id = ""; }, (s) => {  // one signer: the routed one
    who.textContent = s.name ? `${s.name}, ${s.role}` : (s.reason || "No signer");
    rule.textContent = s.rule || "";
    face.textContent = s.name ? initials(s.name) : "";
  });
  const opts = el("div", { class: "opts" , hidden: true }, (d.options || []).map((x) => el("div", { class: "alt" }, el("b", {}, x.label), el("span", {}, x.text))));
  o.replaceChildren(el("div", { class: "dcard", role: "dialog", "aria-modal": "true", "aria-label": d.title },
    el("div", { class: "kicker" }, "Decision needed"),
    el("h2", {}, d.title),
    el("div", { class: "rec" }, d.recommendation || ""),
    el("div", { class: "nums" },
      el("div", { class: "prot" }, el("b", {}, d.impact && d.impact.eur ? eur(d.impact.eur) : "–"), el("span", {}, d.impact ? d.impact.text || "protected" : "")),
      el("div", {}, el("b", {}, d.cost && d.cost.eur != null ? eur(d.cost.eur) : "–"), el("span", {}, d.cost ? d.cost.text || "cost" : ""))),
    el("div", { class: "signer" }, face, el("div", {}, who, rule)),
    (d.on_accept || []).length ? el("ul", {}, d.on_accept.map((a) => el("li", {}, a))) : null,
    opts,
    el("div", { class: "acts" }, accept,
      (d.options || []).length ? el("button", { class: "btn", onclick: () => { opts.hidden = !opts.hidden; } }, "Other options") : null,
      el("button", { class: "btn", onclick: () => { S.dismissed.add(d.decision_id); S.decisionOpen = null; o.dataset.id = ""; render(); } }, "Later")),
    err));
  setTimeout(() => accept.focus(), 50);
}
