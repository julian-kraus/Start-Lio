"use strict";
const NAV=["timeline","plan","weak","decisions","mail"];
function renderHeader(){
  const current=route(),launch=S.state&&S.state.launch||{};
  $("brand-name").textContent=t("brand");$("launch-name").textContent=launch.name||"";
  document.title=launch.name?`${t("brand")} · ${launch.name}`:t("brand");
  const tabs=$("tabs");tabs.replaceChildren();
  NAV.forEach(view=>{const tab=button("",()=>setRoute(view),"tab"+(current===view?" on":""));tab.setAttribute("role","tab");tab.setAttribute("aria-selected",String(current===view));tab.append(icon(view),textNode(t("nav."+view)));if(view==="decisions"){const count=openDecisions().length;if(count){const badge=document.createElement("span");badge.className="count";badge.textContent=String(count);tab.append(badge)}}tabs.append(tab)});
  $("deadline").replaceChildren(icon("clock"),textNode(launch.deadline?`${t("deadline")} ${formatDeadline(launch.deadline)}`:""));$("deadline").hidden=!launch.deadline;
  const avatar=launch.operator_initials||launch.approver_initials||"?";$("avatar").textContent=avatar;$("rules-link").replaceChildren(icon("rules"),textNode(t("agent_rules")));$("rules-link").onclick=()=>{closeProfile();setRoute("rules")};
  document.querySelectorAll(".lang").forEach(node=>{node.classList.toggle("on",node.dataset.lang===S.language);node.setAttribute("aria-pressed",String(node.dataset.lang===S.language))});
}
function closeProfile(){S.menu=false;$("profile-menu").hidden=true;$("avatar").setAttribute("aria-expanded","false")}
$("avatar").addEventListener("click",event=>{event.stopPropagation();S.menu=!S.menu;$("profile-menu").hidden=!S.menu;$("avatar").setAttribute("aria-expanded",String(S.menu))});document.addEventListener("click",closeProfile);
