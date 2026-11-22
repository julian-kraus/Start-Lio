"use strict";
(function(){
  const registry=window.LOOKAHEAD_VIEWS=window.LOOKAHEAD_VIEWS||{};
  let page=0;
  const tr=(t,k)=>t("decisions."+k);
  const time=value=>value && Number.isFinite(Date.parse(value)) ? fmt(value) : "";
  const money=value=>typeof value==="number" && Number.isFinite(value) ? eur(value) : "";
  function stat(label,value,note,cls){return el("div",{},el("span",{},label),el("b",{class:cls||""},value),note?el("small",{},note):null)}
  function detail(decision,t){
    const close=()=>$('modal-root').replaceChildren();
    const box=el("div",{class:"decision-detail",role:"dialog","aria-modal":"true"},
      el("h2",{class:"h-sm"},decision.title||""),el("p",{},decision.recommendation||""),
      el("p",{},decision.reason||""),el("p",{},decision.decided_by||""),
      el("button",{class:"btn btn-secondary",onclick:close},tr(t,"cancel")));
    $('modal-root').replaceChildren(el("div",{class:"decision-backdrop",onclick:e=>{if(e.target===e.currentTarget)close()}},box));
  }
  function card(d,t){
    const outer=el("article",{class:"dc2"});
    const badge=d.urgency?el("span",{class:"badge risk"},d.urgency):null;
    outer.append(el("div",{class:"dc2-meta"},badge,el("span",{class:"dc2-kind"},d.type||""),el("span",{class:"dc2-due"},time(d.deadline||d.decide_by))));
    outer.append(el("h3",{class:"dc2-t"},d.title||""),el("p",{class:"dc2-sum"},d.recommendation||d.subtitle||""));
    const tiles=el("div",{class:"dc2-kpi"},
      stat(tr(t,"pay"),money(d.cost && d.cost.eur),d.cost && d.cost.text),
      stat(tr(t,"refunds"),money(d.impact && d.impact.eur),d.impact && d.impact.text,"bad"),
      stat(tr(t,"now"),d.sourced_now && (d.sourced_now.text||money(d.sourced_now.eur)),""));
    outer.append(tiles);
    const plan=el("div",{class:"dc2-plan"},el("h4",{},tr(t,"plan")));
    for(const item of Array.isArray(d.plan_steps)?d.plan_steps:[])plan.append(el("p",{},item.label||item.text||""));
    const why=el("div",{class:"dc2-why"},el("h4",{},tr(t,"why")));
    for(const item of Array.isArray(d.reasons)?d.reasons:[]){
      const text=typeof item==="string"?item:item.text||item.label||"";
      const line=el("p",{},text);if(item&&typeof item==="object"&&item.source)line.title=JSON.stringify(item.source);why.append(line);
    }
    outer.append(el("div",{class:"dc2-cols"},plan,why));
    const signer=d.signer||{}, signerText=[signer.name,signer.role,signer.rule].filter(Boolean).join(" · ");
    const error=el("div",{class:"err",role:"alert"});
    const accept=acceptButton(d,error,()=>{});accept.textContent=tr(t,"accept");accept.className="btn btn-primary";
    if(!signer.name)accept.disabled=true;
    const reject=el("button",{class:"btn btn-secondary",type:"button",disabled:!signer.name,onclick:()=>openRejectDialog(d,t)},tr(t,"reject"));
    outer.append(el("div",{class:"dc2-foot"},el("div",{class:"dc-sign"},tr(t,"signer")," ",el("b",{},signerText)),
      el("div",{class:"dq-btns2"},reject,accept)),error);
    return outer;
  }
  function render(root,state,t){
    const decisions=state.decisions||[], open=decisions.filter(d=>d.status==="open"), done=decisions.filter(d=>d.status!=="open");
    page=Math.min(Math.max(page,0),Math.max(0,open.length-1));
    const pager=el("span",{class:"dpager"},el("button",{class:"btn btn-secondary",disabled:page===0,"aria-label":tr(t,"previous"),onclick:()=>{page--;renderAll()}},"‹"),
      el("span",{},open.length?`${page+1} / ${open.length}`:""),el("button",{class:"btn btn-secondary",disabled:page>=open.length-1,"aria-label":tr(t,"next"),onclick:()=>{page++;renderAll()}},"›"));
    const waiting=el("section",{class:"card"},el("div",{class:"between"},el("h2",{class:"h-sm"},tr(t,"waiting")),pager));
    const grid=el("div",{class:"dgrid"});if(open[page])grid.append(card(open[page],t));waiting.append(grid);root.append(waiting);
    const table=el("table",{class:"table"}),head=el("tr",{},["decision","by","impact","amount","time","result"].map(k=>el("th",{},tr(t,k))),el("th",{},""));
    table.append(el("thead",{},head));const body=el("tbody");
    done.forEach(d=>body.append(el("tr",{},el("td",{},d.title||""),el("td",{},d.decided_by||""),
      el("td",{},money(d.impact&&d.impact.eur)),el("td",{},money(d.cost&&d.cost.eur)),el("td",{},time(d.decided_at)),
      el("td",{},el("span",{class:"badge neutral"},d.status||"")),el("td",{},el("button",{class:"btn btn-text",onclick:()=>detail(d,t),"aria-label":d.title||tr(t,"decided")},"›")))));
    table.append(body);root.append(el("section",{class:"card"},el("div",{class:"between"},el("h2",{class:"h-sm"},tr(t,"decided")),el("span",{class:"badge neutral"},done.length)),table));
  }
  function renderAll(){window.render()}
  registry.decisions=render;
}());
