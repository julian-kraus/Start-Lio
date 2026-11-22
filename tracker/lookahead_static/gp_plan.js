// Grand Plan Lio "The agent's plan" view: the prototype's flow (analysis/design/grand-plan-lio), with every node's
// text and state taken from the Lookahead records. Nodes the records do not support are left out, not invented.
"use strict";
const GI = {
  brief: '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9.5 4V3h5v1"/><circle cx="12" cy="13" r="4"/><circle cx="12" cy="13" r="1" fill="currentColor"/>',
  hp: '<path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z"/><path d="M8 12h2l1-2 2 4 1-2h2"/>',
  aim: '<circle cx="12" cy="12" r="7"/><path d="M12 2.5v4M12 17.5v4M2.5 12h4M17.5 12h4"/><circle cx="12" cy="12" r="1.2" fill="currentColor"/>',
  shield: '<path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"/><path d="M12 9v6M9 12h6"/>',
  flag: '<path d="M6 21V3.5"/><path d="M6 4h11l-2.5 3.5L17 11H6"/><path d="M4 21h5"/>',
  dmg: '<path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z"/><path d="M12.6 7.6l-2 3.2 3 1.8-2 3.6"/>',
  pad: '<path d="M7.5 8.5h9a4.2 4.2 0 0 1 4.1 5l-.6 3.2a2 2 0 0 1-3.5.9L14.8 15H9.2l-1.7 2.6a2 2 0 0 1-3.5-.9l-.6-3.2a4.2 4.2 0 0 1 4.1-5z"/><path d="M8.5 11v3M7 12.5h3"/><circle cx="15.5" cy="11.5" r=".9" fill="currentColor"/><circle cx="17" cy="13.5" r=".9" fill="currentColor"/>',
  trophy: '<path d="M8 4h8v5a4 4 0 0 1-8 0z"/><path d="M8 6H5.2a3 3 0 0 0 3 4.2M16 6h2.8a3 3 0 0 1-3 4.2"/><path d="M12 13v4M8.5 20.5h7M10 17h4v3.5h-4z"/>',
  star: '<path d="M12 2.8l2.8 5.9 6.4.8-4.7 4.4 1.2 6.4L12 17.2l-5.7 3.1 1.2-6.4L2.8 9.5l6.4-.8z" fill="currentColor"/>',
};
const gi = (k, s = 20) => `<svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${GI[k]}</svg>`;
const IK = {1: "brief", 2: "hp", 3: "aim", 4: "shield", 5: "flag", ad: "shield", ac: "dmg", b: "pad", g: "trophy"};

function planFacts(S) {
  const ev = S.events || [], sup = S.suppliers || [], dec = (S.decisions || [])[0];
  const A = sup.find(s => s.scenario);
  const backupId = (dec && dec.supplier_id) || ((S.promises || []).find(p => A && p.supplier_id !== A.supplier_id) || {}).supplier_id;
  const B = sup.find(s => s.supplier_id === backupId);
  const goals = ev.filter(e => e.type === "goal");
  return {ev, A, B, dec,
    weak: ev.find(e => e.type === "agent" && e.impact && e.impact.units),
    hedge: ev.find(e => e.type === "agent" && !(e.impact && e.impact.units)),
    canc: ev.find(e => e.type === "cancellation"),
    final: goals.length > 1 ? goals[goals.length - 1] : null};
}

function planView(S) {
  const el = document.getElementById("v-plan");
  if (!el || !S.launch) return;
  const f = planFacts(S), lanes = (S.plan || {}).lanes || [], P = S.promises || [];
  const pct = s => s && s.score ? Math.round(s.score.value * 100) + "%" : "—";
  const proven = P.filter(p => p.state === "proven").length, waiting = P.filter(p => p.state === "waiting").length;
  const crit = lanes.filter(l => l.gap > 0 || l.status === "covered");
  const st = {
    1: S.goal ? "done" : "cur",
    2: (S.suppliers || []).length ? "done" : "todo",
    3: lanes.length ? "done" : (S.goal ? "cur" : "todo"),   // a plan with coverage only has ranked nothing
    4: f.hedge ? "done" : (lanes.length ? "cur" : "todo"),
    5: proven ? "done" : (waiting ? "cur" : "todo"),
    ad: f.canc ? "off" : "alt",
    ac: f.canc ? "bad" : "alt",
    b: f.dec ? ({open: "cur", accepted: "done"}[f.dec.status] || "off") : "alt",   // a rejection finishes nothing
    g: f.final ? (f.final.severity === "ok" ? "goal" : "todo") : "todo",
  };
  const cover = ((S.plan || {}).coverage) || {}, of = s => [].concat((s && s.score && s.score.sources) || []);
  const from = {   // the sources behind each node's numbers
    1: f.weak && f.weak.impact.sources,
    2: f.A && f.B ? of(f.B).concat(of(f.A), {scenario_input: f.A.scenario}) : null,
    4: f.hedge && f.hedge.impact && f.hedge.impact.sources,
    ac: f.canc && f.canc.impact && f.canc.impact.sources,
    b: f.dec && f.dec.cost && f.dec.cost.sources,
    g: cover.sources,
  };
  const box = (id, area, t, x, extra = "") => {
    const k = st[id];
    return `<div class="pf ${k}" id="pf-${id}"${from[id] ? srcTip(from[id]) : ""} style="grid-area:${area}"><span class="pf-tile">${gi(IK[id])}${id === "g" && k === "goal" ? `<i class="pf-st s1">${gi("star", 10)}</i><i class="pf-st s2">${gi("star", 9)}</i><i class="pf-st s3">${gi("star", 8)}</i>` : ""}</span>
      <div class="pf-tx"><b>${esc(t)}</b><span>${esc(x)}</span>${extra}</div>
      ${k === "done" ? `<span class="pf-ic">${ic("check")}</span>` : k === "cur" ? `<span class="badge b-a pf-bd">Now</span>` : k === "bad" ? `<span class="badge b-r pf-bd">Happened</span>` : ""}</div>`;
  };
  const nf = proven;
  const flags = `<span class="pf-flags">${P.map((_, i) => i).map(i => `<i class="${i < nf ? "on" : ""}" style="--d:${i * .18}s">${gi("flag", 13)}</i>`).join("")}<em>Checkpoints ${nf} / ${P.length}</em></span>`;
  const units = f.canc && f.canc.impact ? f.canc.impact.units : null;
  const needed = (((S.plan || {}).coverage) || {}).needed;
  const left = needed ? Math.max(0, 100 - units / needed * 100) : null;   // the copies not at risk
  const hp = st.ac === "bad" && units ? `<span class="pf-hp">${left == null ? "" : `<i><b style="width:${left}%"></b></i>`}<em>−${num(units)}</em></span>` : "";
  const aName = f.A ? f.A.name : "The first offer", bName = f.B ? f.B.name : "the backup";
  // the outcomes of a hedge exist only once a hedge was placed: before that the plan names no supplier's future
  const branches = !!(f.hedge && f.A);
  const next = P.find(p => p.state === "waiting");
  const flow = `<div class="pf-grid" id="pfGrid"><svg class="pf-svg" id="pfSvg"></svg>
    ${box(1, "1/2", "Goal interview", S.goal ? `${f.weak ? "At stake " + eur(f.weak.impact.eur) + ", " : ""}deadline ${dhm(S.launch.deadline)}` : "Waiting for the goal")}
    ${box(2, "2/2", "Supplier reliability", f.A && f.B ? `${bName} ${pct(f.B)} from its order history; ${aName} ${pct(f.A)}. ${f.A.scenario}` : `${(S.suppliers || []).length} suppliers scored from their order history`)}
    ${box(3, "3/2", "Critical points", crit.length ? crit.map(l => l.name).join(", ") + " critical" : "Ranked once the goal is set")}
    ${box(4, "4/2", f.hedge ? "Hedge placed" : "Hedge", f.hedge ? f.hedge.title : "Waiting for an offer to hedge")}
    ${box(5, "5/2", "Check-in: critical points on schedule?", next ? `Next proof: ${next.what}, ${dhm(next.next_check || next.due)}` : "Every promise needs a proof", flags)}
    ${branches ? box("ad", "6/1", f.canc ? `${aName} did not deliver` : `If ${aName} delivers`, f.canc ? "The original plan no longer holds" : `Release the ${bName} lot`) : ""}
    ${branches ? box("ac", "6/2", f.canc ? `${aName} cancelled` : `If ${aName} fails`, f.canc ? (units ? `${num(units)} copies at risk; ${bName} already moving` : `${bName} already moving`) : `${bName} carries the copies`, hp) : ""}
    ${f.dec || branches ? box("b", "7/2", f.dec ? f.dec.title : `Confirm the ${bName} lot`, f.dec ? `${eur(f.dec.cost && f.dec.cost.eur)} to pay, ${f.dec.status === "open" ? `signer: ${signerText(signerOf(f.dec))}` : `${f.dec.status === "rejected" ? "rejected" : "decided"} by ${f.dec.decided_by || "nobody on record"}`}` : `Only if ${aName} fails: one click for the signer`, st.b === "cur" ? `<button class="btn btn-primary pf-go" data-goview="decisions">Review and confirm</button>` : "") : ""}
    ${box("g", branches || f.dec ? "8/2" : "6/2", f.final ? (f.final.severity === "ok" ? "Goal met" : "Goal not met yet") : "Goal", f.final ? f.final.title : `All ${num((S.plan || {}).coverage ? S.plan.coverage.needed : null)} ${S.launch.name} copies ship on time`)}
    <div class="pf-loop ${f.canc ? "on" : ""}" style="grid-area:3/3/6/4"><div><b>${f.canc ? "LEVEL UP" : "Learning"}</b><span>${f.canc && f.A ? `${esc(aName)}'s score is now ${pct(f.A)} after the broken promise, so the next hedge is sized with it` : "Every outcome updates the supplier scores"}</span>${f.canc ? '<i class="pf-xp"><b></b></i>' : ""}</div></div>
  </div>`;
  const badge = f.final ? (f.final.severity === "ok" ? B("g", "Goal met") : B("r", "Next weak spot open"))
    : f.dec && f.dec.status === "open" ? B("r", "Needs your sign-off")
    : f.dec && f.dec.status === "rejected" ? B("r", "Rejected, needs a new plan") : B("a", "On track");
  el.innerHTML = `<section class="card pl-canvas"><div class="between"><h2 class="h-sm">The agent's plan</h2>${badge}</div>${flow}</section>`;
  el.querySelectorAll("[data-goview]").forEach(b => b.onclick = () => show(b.dataset.goview));
  requestAnimationFrame(pfWires);
}

function pfWires() {
  const g = document.getElementById("pfGrid"), svg = document.getElementById("pfSvg");
  if (!g || !g.offsetParent) return;
  const G = g.getBoundingClientRect();
  const R = id => { const r = document.getElementById("pf-" + id).getBoundingClientRect();
    return {l: r.left - G.left, r: r.right - G.left, t: r.top - G.top, b: r.bottom - G.top, cx: (r.left + r.right) / 2 - G.left, cy: (r.top + r.bottom) / 2 - G.top}; };
  const paths = [], A = (d, c = "") => paths.push(`<path d="${d}" class="${c}" marker-end="url(#pfArr${c.includes("hot") || c.includes("crit") ? "H" : c.includes("red") ? "R" : ""})"/>`);
  const down = (a, b, c) => { const x = R(a), y = R(b);
    if (Math.abs(x.cx - y.cx) < 2) A(`M${x.cx} ${x.b} V${y.t - 2}`, c); else { const m = (x.b + y.t) / 2; A(`M${x.cx} ${x.b} V${m} H${y.cx} V${y.t - 2}`, c); } };
  const has = id => !!document.getElementById("pf-" + id);
  const st = id => has(id) ? document.getElementById("pf-" + id).className.replace("pf ", "") : "";
  [[1, 2], [2, 3], [3, 4], [4, 5]].forEach(([a, b]) => down(a, b));
  { const a = R(3), b = R(5), x = a.l - 26; A(`M${a.l} ${a.cy} H${x} V${b.cy} H${b.l - 2}`, "crit");
    paths.push(`<text x="${x - 8}" y="${(a.cy + b.cy) / 2 - 4}" text-anchor="end" class="pf-lab"><tspan x="${x - 8}">Each critical point</tspan><tspan x="${x - 8}" dy="16">has a deadline</tspan></text>`); }
  const hit = st("ac") === "bad";
  if (has("ac")) {   // the hedge's two outcomes, then the decision
    down(5, "ad", hit ? "dim" : "alt"); down(5, "ac", hit ? "red" : "alt"); down("ac", "b", hit ? "red" : "alt");
    { const a = R("ad"), gg = R("g"); A(`M${a.cx} ${a.b} V${gg.cy} H${gg.l - 2}`, hit ? "dim" : "alt"); }
    { const a = R("ac"), t = R(2), x = G.width - 6; A(`M${a.r} ${a.cy} H${x} V${t.cy} H${t.r + 2}`, "loop" + (hit ? " on" : "")); }
  } else if (has("b")) down(5, "b", "alt");
  if (has("b")) down("b", "g", st("b") === "done" ? "" : st("b") === "cur" ? "hot" : "alt");
  else down(5, "g", "alt");
  svg.setAttribute("width", G.width); svg.setAttribute("height", G.height);
  svg.innerHTML = `<defs>${[["", "#b9bdc8"], ["H", "#4d4b9e"], ["R", "#d5544a"]].map(([k, c]) => `<marker id="pfArr${k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M1 1 L9 5 L1 9" fill="none" stroke="${c}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></marker>`).join("")}</defs>${paths.join("")}`;
}
addEventListener("resize", () => pfWires());
