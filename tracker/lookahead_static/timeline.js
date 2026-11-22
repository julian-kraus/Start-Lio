"use strict";
(function () {
  const registry = window.LOOKAHEAD_VIEWS = window.LOOKAHEAD_VIEWS || {};
  const label = (t, key) => t("timeline." + key);
  const validTime = value => { const time = Date.parse(value); return Number.isFinite(time) ? time : null; };
  const percent = (value, start, end) => Math.max(0, Math.min(100, (value - start) / (end - start) * 100));
  function eventIcon(event) {
    const type=String(event.type || "").toLowerCase();
    const path=/cancel|fail|delay/.test(type)?"m21.7 18-8-14a2 2 0 0 0-3.4 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3z M12 9v4M12 17h.01":
      /mail|email|offer/.test(type)?"M3 5h18v14H3z M3 7l9 6 9-6":
      /load|freight|ship/.test(type)?"M2 5h12v12H2z M14 9h4l4 4v4h-8 M6 17v2 M18 17v2":
      /confirm|reserv/.test(type)?"M20 6 9 17l-5-5":"M12 3v3M12 18v3M3 12h3M18 12h3 M10 12h4";
    return sv("svg",{viewBox:"0 0 24 24",fill:"none",stroke:"currentColor","stroke-width":"2","stroke-linecap":"round","stroke-linejoin":"round","aria-hidden":"true"},sv("path",{d:path}));
  }
  function renderTimeline(root, state, t) {
    const card = el("section", {class:"card tlcard"});
    const grid = el("div", {class:"tlgrid"});
    const main = el("div", {class:"tlmain"}, el("div", {class:"between"}, el("h2", {class:"h-sm"}, label(t,"title"))));
    const clock = validTime(state.clock && state.clock.now), deadline = validTime(state.launch && state.launch.deadline);
    const suppliers = state.suppliers || [], events = state.events || [];
    if (clock !== null && deadline !== null) {
      const eventTimes = events.filter(e => e.supplier_id || e.lane).map(e => validTime(e.at)).filter(v => v !== null);
      const start = Math.min(clock, deadline, ...eventTimes);
      const last = Math.max(clock, deadline, ...eventTimes);
      const end = last + Math.max(1, last - start) / 5;
      const chart = el("div", {class:"tl", style:"margin-top:40px"});
      const lanes = el("div", {class:"lanes"});
      const past = el("div", {class:"after", style:`left:${percent(deadline,start,end)}%`});
      const deadlineLine = el("div", {class:"vline dead", style:`left:${percent(deadline,start,end)}%`},
        el("span", {class:"vlab"}, label(t,"deadline")));
      const now = el("div", {class:"now", style:`left:${percent(clock,start,end)}%`}, el("span", {}, label(t,"now")));
      lanes.append(past, deadlineLine, now);
      for (const supplier of suppliers) {
        const isCritical = (state.plan && state.plan.lanes || []).some(l =>
          (l.supplier_id === supplier.supplier_id || (supplier.parts || []).includes(l.lane_id)) &&
          (l.status === "critical" || l.status === "at_risk"));
        const row = el("div", {class:"lane" + (isCritical ? "" : " dim")});
        row.append(el("button", {class:"lname", onclick:()=>select({type:"supplier",id:supplier.supplier_id})},
          el("b", {}, supplier.name || supplier.supplier_id)));
        events.filter(event => event.supplier_id === supplier.supplier_id || event.lane === supplier.supplier_id).forEach(event => {
          const at=validTime(event.at); if(at===null || at<start || at>end)return;
          const type=String(event.type || "").toLowerCase();
          const kind=event.severity === "critical" || /cancel|fail|delay/.test(type) ? " bad" : /loaded|received|confirm/.test(type) ? " good" : event.producer && String(event.producer).startsWith("agent") ? " agent" : "";
          const chip=el("button", {class:"ev"+kind,style:`left:${percent(at,start,end)}%`,onclick:()=>select({type:"event",id:event.id})},
            el("span", {class:"pin"}, eventIcon(event)), event.title || "");
          row.append(chip);
        });
        lanes.append(row);
      }
      chart.append(lanes);
      const axis=el("div", {class:"axis",style:"margin-top:28px"});
      for(let i=0;i<=4;i++){
        const time=start+(end-start)*i/4;
        axis.append(el("span", {class:"tick",style:`left:${i*25}%`},fmt(new Date(time).toISOString())));
      }
      chart.append(axis); main.append(chart);
    }
    const open=(state.decisions || []).filter(d=>d.status === "open");
    const aside=el("aside", {class:"dq","aria-label":label(t,"decisions")});
    const list=el("div", {class:"dq-list"});
    open.forEach(decision=>list.append(decisionCard(decision,t)));
    const all=el("button", {class:"btn btn-text dq-all",onclick:()=>setRoute("decisions")}, label(t,"all"));
    aside.append(el("div", {class:"dq-in"}, el("div", {class:"between"},
      el("h3", {class:"dq-h"}, label(t,"decisions"),el("span", {class:"count"},open.length)),all),list));
    grid.append(main,aside);card.append(grid);root.append(card);
    root.append(feed(state.events || [],t));
  }
  function decisionCard(d,t) {
    const box=el("div", {class:"dq-card"},el("div", {class:"dq-top"}, d.urgency || "", d.deadline ? fmt(d.deadline) : ""),
      el("div", {class:"dq-t"},d.title || ""), el("div", {class:"dq-cause"},d.recommendation || ""));
    if(d.cost && d.cost.eur != null) box.append(el("div", {class:"dq-cost"},label(t,"pay"),el("b",{},eur(d.cost.eur))));
    if(d.impact && d.impact.eur != null) box.append(el("div", {class:"dq-cost"},label(t,"risk"),el("b",{},eur(d.impact.eur))));
    if(d.signer && (d.signer.name || d.signer.role))box.append(el("div", {class:"dq-cause"},[d.signer.name,d.signer.role].filter(Boolean).join(", ")));
    const error=el("div", {class:"err"});
    const accept=acceptButton(d,error,()=>{});accept.className="btn btn-primary";
    if(!d.signer || !d.signer.name)accept.disabled=true;
    const reject=el("button", {class:"btn btn-secondary",type:"button",disabled:true,title:label(t,"reject_unavailable")},label(t,"reject"));
    box.append(el("div", {class:"dq-btns2"},reject,accept),error);return box;
  }
  function feed(events,t) {
    const list=el("ul", {class:"feed",style:"margin-top:18px"});
    [...events].sort((a,b)=>(validTime(b.at)||0)-(validTime(a.at)||0)).forEach(event=>{
      const bad=event.severity === "critical" || /cancel|fail|delay/.test(event.type || "");
      const when=validTime(event.at) === null ? "" : fmt(event.at);
      list.append(el("li", {class:bad?"k-bad":""},el("span",{class:"ft"},when),el("span",{class:"fn"},eventIcon(event)),el("span",{class:"fx"},event.title || "")));
    });
    return el("section",{class:"card"},el("h2",{class:"h-sm"},label(t,"feed")),list);
  }
  registry.timeline=renderTimeline;
}());
