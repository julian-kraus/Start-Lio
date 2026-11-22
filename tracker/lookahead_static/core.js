"use strict";
const S={state:null,view:"timeline",language:localStorage.getItem("lookahead-language")||"en",strings:{},menu:false};
const $=id=>document.getElementById(id);
const icon=(name,large=false)=>{const paths={timeline:"M4 18V6m5 12V9m5 9V4m5 14V8",plan:"M5 5h14v14H5zM9 9h6M9 13h4",weak:"M4 5h16M4 12h16M4 19h16",decisions:"M5 5h14v14H5zM8 12l2 2 5-5",mail:"M4 6h16v12H4zM4 7l8 6 8-6",clock:"M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16zm0 4v4l3 2",rules:"M5 4h14v16H5zM8 8h8M8 12h8M8 16h5",chevron:"m8 10 4 4 4-4"};const n=document.createElementNS("http://www.w3.org/2000/svg","svg");n.setAttribute("class","ic"+(large?" lg":""));n.setAttribute("viewBox","0 0 24 24");n.setAttribute("aria-hidden","true");const p=document.createElementNS("http://www.w3.org/2000/svg","path");p.setAttribute("d",paths[name]||paths.timeline);n.append(p);return n};
function textNode(value){return document.createTextNode(value==null?"":String(value))}
function button(label,handler,classes=""){const b=document.createElement("button");b.className=classes;b.append(textNode(label));if(handler)b.addEventListener("click",handler);return b}
function formatDeadline(value){if(!value)return "";const date=new Date(value);if(Number.isNaN(date.getTime()))return String(value);return new Intl.DateTimeFormat(S.language==="zh"?"zh-CN":"en-GB",{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit",hour12:false}).format(date)}
function openDecisions(){return (S.state&&S.state.decisions||[]).filter(d=>d.status==="open")}
async function loadState(){try{const r=await fetch("/api/lookahead/state",{headers:{Accept:"application/json"}});if(!r.ok)throw new Error(r.statusText);S.state=await r.json()}catch(_){S.state=S.state||{launch:null,goal:null,plan:null,decisions:[],mode:"live"}}render()}
function route(){const match=location.hash.match(/^#v=(timeline|plan|weak|decisions|mail|rules)$/);return match?match[1]:"timeline"}
function setRoute(view){const next="#v="+view;if(location.hash===next)render();else history.pushState({view},"",next)}
window.addEventListener("hashchange",render);window.addEventListener("popstate",render);
