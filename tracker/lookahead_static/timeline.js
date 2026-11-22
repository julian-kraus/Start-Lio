"use strict";
// ---------- banner ----------
function riskBanner() {
  const st = S.state, cov = st.plan && st.plan.coverage;
  if (!cov || cov.met || cov.hedged || cov.covered >= cov.needed) return null;
  const ev = [...st.events].reverse().find((e) => e.severity === "risk" && e.impact && e.impact.eur);
  if (!ev) return null;
  const big = el("span", { class: "big" }, eur(ev.impact.eur));
  const b = el("div", { class: "banner" + (S.newIds.has(ev.id) ? " new" : ""), onclick: () => select({ type: "event", id: ev.id }) },
    big, el("div", { class: "txt" }, el("b", {}, ev.title), el("br"),
      ev.impact.units ? `${num(ev.impact.units)} ${cov.unit} at risk` : "", openDecisions().length ? " · one decision closes it" : ""));
  if (S.newIds.has(ev.id)) countUp(big, ev.impact.eur);
  return b;
}
function countUp(node, target) {
  const t0 = performance.now(), dur = 1200;
  const step = (t) => { const k = Math.min(1, (t - t0) / dur); node.textContent = eur(target * (1 - Math.pow(1 - k, 3))); if (k < 1) requestAnimationFrame(step); };
  requestAnimationFrame(step);
}

// ---------- timeline ----------
const ICONS = {
  email_out: { d: "M3 8 H12 M9 5 L12 8 L9 11", c: "var(--agent)" },
  email_in: { d: "M13 8 H4 M7 5 L4 8 L7 11", c: "var(--supply)" },
  document: { d: "M5 3 H9.5 L11 4.5 V13 H5 Z M7 7 H9.5 M7 9.5 H9.5", c: "var(--supply)" },
  offer_in: { d: "M8 4 V12 M4 8 H12", c: "var(--supply)" },
  delay: { d: "M8 4.5 V8 L10.5 9.5 M8 3 A5 5 0 1 0 8.01 3", c: "var(--warn)" },
  cancellation: { d: "M5 5 L11 11 M11 5 L5 11", c: "var(--risk)" },
  check_in: { d: "M4.5 8.5 L7 11 L11.5 5.5", c: "var(--ok)" },
  decision: { d: "M8 3.5 L12.5 8 L8 12.5 L3.5 8 Z", c: "var(--warn)" },
  goal: { d: "M5 13 V3 M5 3.5 H11.5 L10 6 L11.5 8.5 H5", c: "var(--ok)" },
  agent: { d: "M8 2.5 L9.2 6.8 L13.5 8 L9.2 9.2 L8 13.5 L6.8 9.2 L2.5 8 L6.8 6.8 Z", c: "var(--agent)", filled: true },
};
function pinColor(e) {
  if (e.type === "agent" || e.type === "email_out") return "var(--agent)";
  if (e.severity === "risk") return "var(--risk)";
  if (e.severity === "ok") return "var(--ok)";
  return (ICONS[e.type] || ICONS.document).c;
}
function windowRange() {
  const st = S.state, L = st.launch || {};
  const dl = L.deadline ? ms(L.deadline) : nowMs() + 86400000;
  const packs = (L.pack_windows || []).map((w) => ms(w.from));
  let t0 = packs.length ? Math.min(...packs) - 2 * 3600000 : nowMs() - 6 * 3600000;
  let t1 = dl + 3 * 3600000;
  if (S.range === "all") {
    const ts = st.events.map((e) => ms(e.at)).concat(lanes().flatMap((l) => (l.bars || []).flatMap((b) => [ms(b.start), ms(b.usable_at)])));
    if (ts.length) { t0 = Math.min(t0, Math.min(...ts) - 3600000); t1 = Math.max(t1, Math.max(...ts) + 3600000); }
  }
  t0 = Math.min(t0, nowMs() - 3600000);
  return [t0, t1];
}

function viewTimeline() {
  const st = S.state;
  const wrap = el("div", { class: "view" });
  const banner = riskBanner();
  if (banner) wrap.append(banner);
  const box = el("div", { class: "tl-wrap" });
  box.append(el("div", { class: "tl-tools" },
    st.plan ? el("span", {}, st.plan.summary || "") : el("span", {}, "No plan yet"),
    el("span", { style: "flex:1" }),
    el("span", { class: "seg" },
      el("button", { "aria-pressed": String(S.range === "launch"), onclick: () => { S.range = "launch"; render(); } }, "Launch window"),
      el("button", { "aria-pressed": String(S.range === "all"), onclick: () => { S.range = "all"; render(); } }, "Everything")),
  ));
  const width = Math.max(640, ($("stage").clientWidth || 1200) - 42);
  box.append(drawTimeline(width));
  if (!st.goal && st.launch && (st.launch.goal_questions || []).length) {
    box.append(el("div", { class: "empty-goal" },
      el("h2", {}, "What is the goal, and how far will you go?"),
      el("p", {}, "Lookahead prices every risk against your goal. Four answers, then it finds the weak spots."),
      el("button", { class: "btn primary big", onclick: openGoal }, "Start goal interview ", el("kbd", {}, "G"))));
  }
  wrap.append(box);
  return wrap;
}

function drawTimeline(W) {
  const st = S.state, L = st.launch || {};
  const LABEL = W < 800 ? 128 : 196, R = 18, AX = 34;
  const [t0, t1] = windowRange();
  const x = (t) => LABEL + ((t - t0) / (t1 - t0)) * (W - LABEL - R);
  const inWin = (t) => t >= t0 && t <= t1;
  const evs = st.events;
  // world lane: place labels newest first into rows without overlap
  const placed = [], rows = [[], [], [], []], earlier = [];
  const byNew = [...evs].sort((a, b) => ms(b.at) - ms(a.at) || b.id - a.id);
  for (const e of byNew) {
    const t = ms(e.at);
    if (t < t0) { earlier.push(e); continue; }
    const px = x(t);
    const txt = e.title.length > 44 ? e.title.slice(0, 43) + "…" : e.title;
    const w = txt.length * 6.4 + 20;
    let row = -1;
    for (let r = 0; r < rows.length; r++) if (!rows[r].some(([a, b]) => px < b + 8 && px + w > a - 8)) { row = r; break; }
    if (row >= 0) rows[row].push([px, px + w]);
    placed.push({ e, px, row, txt });
  }
  const usedRows = Math.max(1, rows.filter((r) => r.length).length);
  const WORLD_TOP = AX + 6, PIN_Y = WORLD_TOP + 14, WORLD_H = 26 + usedRows * 20 + 8;
  const laneList = lanes();
  let y = WORLD_TOP + WORLD_H;
  const laneY = {};
  const laneGeo = laneList.map((l) => {
    const big = ["critical", "hedged", "at_risk"].includes(l.status) || (l.bars || []).length > 0;
    const h = big ? Math.max(92, 40 + (l.bars || []).length * 44) : 30;
    const g = { l, y, h, big };
    laneY[l.lane_id] = g;
    y += h;
    return g;
  });
  const H = Math.max(y + 16, laneList.length ? 260 : 400);
  const svg = sv("svg", { class: "tl", viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": "Launch timeline" });
  const defs = sv("defs", {},
    sv("pattern", { id: "hatch", width: 7, height: 7, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" },
      sv("rect", { width: 7, height: 7, fill: "var(--risk-bg)" }), sv("line", { x1: 0, y1: 0, x2: 0, y2: 7, stroke: "var(--ghost)", "stroke-width": 1.4, opacity: .55 })),
    sv("clipPath", { id: "plot" }, sv("rect", { x: LABEL, y: 0, width: W - LABEL - R + 2, height: H })));
  svg.append(defs);
  const plot = sv("g", { "clip-path": "url(#plot)" });

  // night / no-packing shading, hour ticks, day labels
  const packs = (L.pack_windows || []).map((w) => [ms(w.from), ms(w.to)]).sort((a, b) => a[0] - b[0]);
  if (packs.length) {
    let cur = t0;
    for (const [a, b] of packs) { if (a > cur) plot.append(sv("rect", { class: "night", x: x(cur), y: AX, width: x(a) - x(cur), height: H - AX })); cur = Math.max(cur, b); }
    if (cur < t1) plot.append(sv("rect", { class: "night", x: x(cur), y: AX, width: x(t1) - x(cur), height: H - AX }));
  }
  const pxPerH = (W - LABEL - R) / ((t1 - t0) / 3600000);
  const stepH = pxPerH > 60 ? 1 : pxPerH > 26 ? 2 : pxPerH > 12 ? 4 : pxPerH > 5 ? 6 : 12;
  const axis = sv("g", {});
  const startH = Math.ceil(t0 / 3600000) * 3600000;
  for (let t = startH; t <= t1; t += 3600000) {
    const hour = Number(new Intl.DateTimeFormat("en-GB", { timeZone: tz(), hour: "2-digit", hour12: false }).format(new Date(t)));
    if (hour % stepH !== 0) continue;
    plot.append(sv("line", { class: "tick", x1: x(t), x2: x(t), y1: AX, y2: H }));
    if (hour === 0) {
      axis.append(sv("text", { x: x(t) + 4, y: 13, class: "muted", "font-weight": 700 }, fmt(new Date(t).toISOString(), { hour: undefined, minute: undefined, day: "numeric", month: "short" })));
      plot.append(sv("line", { x1: x(t), x2: x(t), y1: 0, y2: H, stroke: "var(--border)" }));
    }
    axis.append(sv("text", { x: x(t), y: 29, "text-anchor": "middle", class: "faint", "font-size": 11 }, String(hour).padStart(2, "0")));
  }
  svg.append(plot);
  svg.append(axis);

  // lanes
  for (const g of laneGeo) {
    const { l } = g;
    const lg = sv("g", { class: "lane" + (S.flashLanes.has(l.lane_id) ? " flash" : "") + (S.hlLane === l.lane_id ? " hl" : "") });
    lg.append(sv("rect", { class: "lane-hl", x: 0, y: g.y, width: W, height: g.h }));
    lg.append(sv("line", { class: "lane-sep", x1: 0, x2: W, y1: g.y, y2: g.y }));
    const label = sv("g", { class: "lane-label", onclick: () => select({ type: "lane", id: l.lane_id }) });
    label.append(sv("text", { x: 14, y: g.y + (g.big ? 22 : 19), class: "lane-name" }, l.name));
    if (g.big) {
      const badge = statusText(l.status);
      const bw = badge.length * 6.6 + 12;
      label.append(sv("rect", { x: 14, y: g.y + 30, width: bw, height: 17, rx: 4, fill: statusColor(l.status, true) }));
      label.append(sv("text", { x: 20, y: g.y + 42, "font-size": 10.5, "font-weight": 700, fill: statusColor(l.status) }, badge.toUpperCase()));
      label.append(sv("text", { x: 14, y: g.y + 66, class: "muted", "font-size": 12 }, `${num(l.usable)} / ${num(l.need)}`,
        l.gap ? sv("tspan", { fill: "var(--risk)", "font-weight": 700 }, `  −${num(l.gap)}`) : null));
    } else {
      label.append(sv("text", { x: LABEL - 10, y: g.y + 19, "text-anchor": "end", class: "faint", "font-size": 11 }, statusText(l.status)));
    }
    lg.append(label);
    const track = sv("g", { "clip-path": "url(#plot)" });
    if (!g.big) track.append(sv("line", { class: "collapsed-line", x1: LABEL + 4, x2: W - R - 4, y1: g.y + 15, y2: g.y + 15 }));
    if (g.big && l.needed_by && inWin(ms(l.needed_by))) {
      const nx = x(ms(l.needed_by));
      track.append(sv("line", { class: "needed", x1: nx, x2: nx, y1: g.y + 4, y2: g.y + g.h - 4 }));
      track.append(sv("text", { class: "needed-txt", x: nx - 5, y: g.y + 14, "text-anchor": "end" }, `needed by ${hm(l.needed_by)}`,
        sv("title", {}, l.needed_by_why || "")));
    }
    (l.bars || []).forEach((b, i) => track.append(drawBar(b, l, x, g.y + 38 + i * 44, inWin)));
    for (const p of st.promises.filter((p) => p.lane === l.lane_id)) {
      const t = ms(p.next_check || p.due);
      if (!inWin(t)) continue;
      const bi = (l.bars || []).findIndex((b) => b.supplier_id === p.supplier_id && b.kind !== "ghost");
      const px = x(t), py = bi >= 0 ? g.y + 38 + bi * 44 : g.y + g.h - 12;
      const col = p.state === "proven" ? "var(--ok)" : p.state === "broken" ? "var(--risk)" : "var(--warn)";
      track.append(sv("path", { class: "promise", d: `M${px} ${py - 6} L${px + 6} ${py} L${px} ${py + 6} L${px - 6} ${py} Z`,
        fill: p.state === "waiting" ? "var(--surface)" : col, stroke: col, onclick: () => select({ type: "promise", id: p.promise_id }) },
        sv("title", {}, `Check-in ${fmt(p.next_check || p.due)}: ${p.what} (${p.state})`)));
    }
    lg.append(track);
    svg.append(lg);
  }

  // world lane
  svg.append(sv("text", { x: 14, y: PIN_Y + 4, class: "lane-name" }, "World"));
  svg.append(sv("text", { x: 14, y: PIN_Y + 20, class: "faint", "font-size": 11 }, "events and actions"));
  const world = sv("g", { "clip-path": "url(#plot)" });
  if (S.sel && S.sel.type === "event") {
    const e = evs.find((v) => v.id === S.sel.id);
    if (e && laneY[e.lane] && inWin(ms(e.at))) {
      const gx = x(ms(e.at)), g = laneY[e.lane];
      world.append(sv("line", { class: "connector", x1: gx, x2: gx, y1: PIN_Y + 10, y2: g.y + g.h / 2 }));
    }
  }
  for (const p of [...placed].reverse()) {
    const { e, px, row, txt } = p;
    const ic = ICONS[e.type] || ICONS.document;
    const isSel = S.sel && S.sel.type === "event" && S.sel.id === e.id;
    const g = sv("g", { class: "pin" + (S.newIds.has(e.id) ? " new" : "") + (isSel ? " sel" : ""), onclick: () => select({ type: "event", id: e.id }) },
      sv("title", {}, `${fmt(e.at)} · ${e.title}`));
    if (row >= 0) {
      const ly = PIN_Y + 22 + row * 20;
      g.append(sv("line", { x1: px, x2: px, y1: PIN_Y + 9, y2: ly - 4, stroke: "var(--border)" }));
      g.append(sv("text", { class: "pin-label", x: px + 4, y: ly + 4, "font-weight": isSel || S.newIds.has(e.id) ? 700 : 400,
        fill: e.severity === "risk" ? "var(--risk)" : "var(--text)" }, txt));
    }
    g.append(sv("circle", { class: "dot", cx: px, cy: PIN_Y, r: 10, fill: pinColor(e) }));
    g.append(sv("path", { d: ic.d, class: ic.filled ? "filled" : "", transform: `translate(${px - 8} ${PIN_Y - 8})` }));
    world.append(g);
  }
  svg.append(world);
  if (earlier.length) {
    svg.append(sv("g", { class: "earlier", onclick: () => { S.range = "all"; render(); } },
      sv("text", { x: 14, y: PIN_Y + 36, fill: "var(--accent)", "font-size": 11 }, `◂ ${earlier.length} earlier, show all`)));
  }

  // now and deadline rules on top
  const top = sv("g", {});
  if (L.deadline && inWin(ms(L.deadline))) {
    const dx = x(ms(L.deadline));
    top.append(sv("line", { class: "deadline-line", x1: dx, x2: dx, y1: AX - 2, y2: H }));
    const t = `Launch ${hm(L.deadline)}`, w = t.length * 6.6 + 12;
    top.append(sv("g", { class: "deadline-tag" }, sv("rect", { x: dx - w / 2, y: AX - 16, width: w, height: 17, rx: 4 }), sv("text", { x: dx, y: AX - 4, "text-anchor": "middle" }, t)));
  }
  const n = nowMs(), nx = x(n);
  const nowG = sv("g", {});
  const t = `Now ${hm(st.clock.now)}`, w = t.length * 6.6 + 12;
  nowG.append(sv("line", { class: "now-line", x1: nx, x2: nx, y1: AX - 2, y2: H }));
  nowG.append(sv("g", { class: "now-tag" }, sv("rect", { x: nx - w / 2, y: AX - 16, width: w, height: 17, rx: 4 }), sv("text", { x: nx, y: AX - 4, "text-anchor": "middle" }, t)));
  top.append(nowG);
  svg.append(top);
  if (S.prevNow != null && S.prevNow !== n) {
    const dxp = x(S.prevNow) - nx;
    requestAnimationFrame(() => nowG.animate([{ transform: `translateX(${dxp}px)` }, { transform: "none" }], { duration: 1400, easing: "cubic-bezier(.4,0,.2,1)" }));
    S.prevNow = n;
  }
  return svg;
}
