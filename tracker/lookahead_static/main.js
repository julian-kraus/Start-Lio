"use strict";
window.LOOKAHEAD_VIEWS=window.LOOKAHEAD_VIEWS||{};
const VIEW_KEYS={timeline:"view.timeline",plan:"view.plan",weak:"view.weak",decisions:"view.decisions",mail:"view.mail",rules:"view.rules"};
function renderGoal(){
  const root=$("goal-summary");root.replaceChildren();
  const goal=S.state&&S.state.goal;
  if(goal&&goal.statement){const q=document.createElement("p");q.className="q";q.textContent=goal.statement;root.append(q)}
}
function renderViews(){
  const current=route();
  document.querySelectorAll(".view[data-view]").forEach(view=>{
    const active=view.dataset.view===current;
    view.classList.toggle("on",active);view.setAttribute("aria-hidden",String(!active));
    if(!active)return;
    view.replaceChildren();
    const renderer=window.LOOKAHEAD_VIEWS[current];
    if(typeof renderer==="function"){renderer(view,S.state,t);return}
    const legacy=current==="timeline"&&typeof viewTimeline==="function"?viewTimeline:
      current==="decisions"&&typeof viewDecisions==="function"?viewDecisions:null;
    if(legacy){const child=legacy();child.style.display="block";view.append(child)}
  });
}
function render(){
  if(!S.state)return;
  S.view=route();renderHeader();renderGoal();renderViews();
  if(typeof renderDrawer==="function"){renderDrawer();$("drawer").classList.toggle("on",!$("drawer").hidden)}
  if(typeof renderOverlay==="function")renderOverlay();
  if(typeof renderOperator==="function")renderOperator();
}
async function loadViewModules(){
  const response=await fetch("/lookahead/static/views/index.json");
  if(!response.ok)throw new Error("Unable to load view list");
  const files=await response.json();
  for(const name of files){
    if(!/^[a-z0-9_]+\.(js|css)$/.test(name))throw new Error("Invalid view file");
    if(name.endsWith(".css")){const link=document.createElement("link");link.rel="stylesheet";link.href="/lookahead/static/"+name;document.head.append(link);continue}
    await new Promise((resolve,reject)=>{const script=document.createElement("script");script.src="/lookahead/static/"+name;script.onload=resolve;script.onerror=reject;document.head.append(script)});
  }
}
function renderClock(){renderHeader()}
async function start(){
  await loadStrings();
  try{await loadViewModules()}catch(error){console.error("Unable to load view modules",error)}
  await load();
  if(!new URLSearchParams(location.search).has("screenshot"))connect();
  if(typeof watchClock==="function")setInterval(watchClock,CLOCK_POLL_MS);
}
document.addEventListener("DOMContentLoaded",start);
