// Grand Plan Lio: the full decision card of the Decisions view, in the prototype's layout
// (analysis/design/grand-plan-lio). Every value comes from the decision, promise and event records;
// nothing here names a part, supplier or scenario. Uses the helpers of gp_app.js.
"use strict";
const LIO_GLYPH = `<svg viewBox="0 0 100 100" aria-hidden="true"><path fill="#fff" d="M20 8h22a9 9 0 0 1 9 9v45h30a9 9 0 0 1 9 9v12a9 9 0 0 1-9 9H20a9 9 0 0 1-9-9V17a9 9 0 0 1 9-9z"/><ellipse cx="25" cy="30" rx="4.6" ry="6" fill="currentColor"/><ellipse cx="38" cy="30" rx="4.6" ry="6" fill="currentColor"/><path d="M26.5 43.5Q31.5 48.5 36.5 43.5" fill="none" stroke="currentColor" stroke-width="3.4" stroke-linecap="round"/></svg>`;

function dcLeft(t) {   // time from the scenario clock to t
  const m = Math.max(0, Math.round((ms(t) - ms(S.clock.now)) / 60000));
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min left` : `${m} min left`;
}
function dcReasons(d) {   // the reasoning events of this decision's lane, oldest first
  return (S.events || []).filter(e => e.lane === d.lane && e.detail && (e.detail.reasoning || []).length)
    .sort((a, b) => ms(a.at) - ms(b.at)).map(e => e.detail.reasoning);
}
function dcWhyCards(d) {   // the agent's "why" steps first (as in the prototype), then the latest step's results
  const all = dcReasons(d), rows = all.flat().filter(r => r.value);
  const whys = rows.filter(r => /^why\b/i.test(r.text));
  const pick = (whys.length ? whys : (all[all.length - 1] || []).filter(r => r.value)).slice(-3);
  return pick.map(r => `<div class="dc3-why"${r.source ? ` title="${esc("Source: " + r.source)}"` : ""}>
    <b>${esc(r.value ? r.text : "")}</b><p>${esc(r.value || r.text)}</p>${r.note ? `<small>${esc(r.note)}</small>` : ""}</div>`).join("");
}
function dcCalc(d) {   // "Show full calculation": every reasoning row with its source
  return dcReasons(d).map(rows => `<dl class="pd-why">${rows.map(r => `<div><dt>${esc(r.text)}</dt><dd>${esc(r.value || "")}${r.note ? ` (${esc(r.note)})` : ""}${r.source ? `<small class="xs subtle" style="display:block;overflow-wrap:anywhere">Source: ${esc(r.source)}</small>` : ""}</dd></div>`).join("")}</dl>`).join("");
}
function dcTrack(d) {   // Plan B: the promises of the decision's supplier on a line from the first one to the deadline
  const lane = (S.promises || []).filter(p => p.lane === d.lane);
  const own = lane.filter(p => p.supplier_id === d.supplier_id);
  const proms = (own.length ? own : lane).filter(p => p.due || p.next_check).sort((a, b) => ms(a.due || a.next_check) - ms(b.due || b.next_check));
  const now = ms(S.clock.now), dead = ms(S.launch.deadline);
  const a = Math.min(now, ...proms.map(p => ms(p.due || p.next_check))), span = Math.max(dead - a, 1);
  const X = t => Math.min(100, Math.max(0, (t - a) / span * 100)).toFixed(2);
  const pts = proms.map((p, i) => {
    const t = ms(p.due || p.next_check), k = p.state === "proven" ? "done" : p.state === "broken" ? "bad" : "open";
    return `<div class="dc3-pt k-${k}${i === 0 && X(t) < 12 ? " first" : ""}" style="left:${X(t)}%"><i>${k === "bad" ? ic("x") : ""}</i><span><b>${esc(dhm(t))}</b>${esc(p.what || "")}</span></div>`;
  }).join("");
  const nowPt = `<div class="dc3-pt k-now" style="left:${X(now)}%"><i>${ic("clock")}</i><span><b>Now</b>${esc(signerOf(d).name ? "Waiting for your confirmation" : "Waiting for a signer")}</span></div>`;
  const end = `<div class="dc3-pt k-end" style="left:100%"><i></i><span><b>${esc(dhm(dead))}</b>Deadline</span></div>`;
  return `<div class="dc3-track"><p class="dc3-track-h"><span>Plan B · ${esc(supName(d.supplier_id))}</span></p>
    <div class="dc3-line"><div class="dc3-fill" style="width:${X(now)}%"></div>${pts}${nowPt}${end}</div></div>`;
}
function dcSigner(d) {
  const s = signerOf(d), ok = canSign(s);
  const head = ok ? `${s.name} can sign this` : signerText(s);
  return `<div class="pd-sign"><span class="act-av${s.name ? "" : " empty"}">${esc(s.name ? initials(s.name) : "?")}</span><span><b>${esc(head)}</b><small>${esc([s.role, s.rule || s.reason].filter(Boolean).join(" · "))}</small></span></div>`;
}
function decCard(d) {
  const s = signerOf(d), cost = d.cost || {}, imp = d.impact || {};
  const ratio = cost.eur > 0 && imp.eur > 0 ? Math.round(imp.eur / cost.eur) : null;
  const badge = s.status === "needs_human" ? B("am", "No signer") : B("r", "Open");
  return `<article class="dc2 dc3" id="dc-${esc(d.decision_id)}"><div class="dc2-meta">${badge}<span class="xs subtle">Raised ${esc(dhm(d.at))}</span>
    <span class="dc2-due">${ic("clock")}<b>Deadline ${esc(dhm(S.launch.deadline))}</b><span class="warn">${esc(dcLeft(S.launch.deadline))}</span></span></div>
  <h3 class="dc2-t">${esc(d.title)}</h3><p class="dc3-sum">${esc(d.recommendation || "")}</p>
  <div class="dc3-body"><div class="dc3-kpis">
    <div class="dc3-kpi"><span>To pay</span><b>${eur(cost.eur)}</b>${ratio ? `<p>Every €1 paid protects €${num(ratio)} in refunds</p>` : ""}<small>${esc(cost.text || "")}</small>${srcLine(cost.sources)}</div>
    <div class="dc3-kpi"><span>Refunds if ignored</span><b class="bad">${eur(imp.eur)}</b>${imp.units ? `<p>${num(imp.units)} copies affected</p>` : ""}<small>${esc(imp.text || "")}</small>${srcLine(imp.sources)}</div></div>
   <section class="dc3-whys"><p class="dc3-lio"><span class="dc3-av">${LIO_GLYPH}</span><span><b>Lio</b> on why this is the move</span></p>${dcWhyCards(d)}</section></div>
  ${dcTrack(d)}
  <div class="dc3-calc" hidden>${dcCalc(d)}</div>
  <div class="dc2-foot">${dcSigner(d)}<button class="btn btn-text dc3-more" data-calc="${esc(d.decision_id)}">${ic("document")}Show full calculation</button>${acts(d)}</div></article>`;
}
document.addEventListener("click", e => {   // the cards are re-rendered on every state, so listen once on the page
  const b = e.target.closest("[data-calc]");
  if (!b) return;
  e.stopPropagation();
  const calc = b.closest(".dc3").querySelector(".dc3-calc");
  calc.hidden = !calc.hidden;
  b.lastChild.textContent = calc.hidden ? "Show full calculation" : "Hide full calculation";
});
