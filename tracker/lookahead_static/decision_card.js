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

function openRejectDialog(decision, translate) {
  const tr = key => translate("decisions." + key);
  const close = () => $("modal-root").replaceChildren();
  const reason = el("textarea", {rows:"3", "aria-label":tr("reason")});
  const error = el("p", {class:"decision-error",role:"alert"});
  const submit = el("button", {class:"btn btn-primary",type:"button",disabled:true}, tr("regenerate"));
  reason.addEventListener("input", () => { submit.disabled = !reason.value.trim(); });
  submit.addEventListener("click", async () => {
    const text=reason.value.trim();
    if(!text){error.textContent=tr("reason_required");return}
    submit.disabled=true;error.textContent="";
    try {
      const fresh=await api(`/api/lookahead/decisions/${encodeURIComponent(decision.decision_id)}`);
      if(!fresh.approval || fresh.approval.status!=="resolved" || !fresh.signer ||
         fresh.signer.name!==(decision.signer && decision.signer.name))throw new Error(tr("signer_unavailable"));
      await api(`/api/lookahead/decisions/${encodeURIComponent(decision.decision_id)}/reject`,
        {approver:decision.signer && decision.signer.name || "",reason:text});
      close();await load();
    } catch (failure) { error.textContent=failure.message;submit.disabled=false; }
  });
  const content=el("div",{class:"reject-box",role:"dialog","aria-modal":"true","aria-label":tr("reject_title")},
    el("p",{class:"reject-title"},tr("reject_title")),el("p",{class:"reject-name"},decision.title||""),
    el("p",{class:"reject-info"},tr("reject_info")),el("label",{class:"reject-field"},tr("reason"),reason),error,
    el("div",{class:"reject-buttons"},el("button",{class:"btn btn-secondary",type:"button",onclick:close},tr("cancel")),submit));
  $("modal-root").replaceChildren(el("div",{class:"decision-backdrop",onclick:event=>{if(event.target===event.currentTarget)close()}},content));
  reason.focus();
}

window.decisionActions = function(decision, translate, error, signerLine) {
  const tr = key => translate("decisions." + key);
  const actions=el("div",{class:"dq-btns2"});
  const unavailable=()=>actions.replaceChildren(
    el("button",{class:"btn btn-secondary",type:"button",disabled:true},tr("reject")),
    el("button",{class:"btn btn-primary",type:"button",disabled:true},tr("accept")));
  unavailable();
  api(`/api/lookahead/decisions/${encodeURIComponent(decision.decision_id)}`)
    .then(routed=>{
      const signer=routed && routed.signer;
      if(!routed.approval || routed.approval.status!=="resolved" || !signer || !signer.name) {
        error.textContent=tr("signer_unavailable");return;
      }
      signerLine.replaceChildren(document.createTextNode([signer.name,signer.role,signer.rule].filter(Boolean).join(" · ")));
      let currentSigner=signer;
      const reject=el("button",{class:"btn btn-secondary",type:"button",onclick:()=>openRejectDialog({...routed,signer:currentSigner},translate)},tr("reject"));
      const accept=acceptButton(routed,error,()=>{},next=>{
        currentSigner=next;
        signerLine.replaceChildren(document.createTextNode([next.name,next.role,next.rule].filter(Boolean).join(" · ")));
        reject.disabled=!next.name;
      });accept.textContent=tr("accept");accept.className="btn btn-primary";
      actions.replaceChildren(reject,accept);
    }).catch(()=>{error.textContent=tr("signer_unavailable")});
  return actions;
};
