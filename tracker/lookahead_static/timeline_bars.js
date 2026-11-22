"use strict";
function drawBar(b, l, x, cy, inWin) {
  const a = ms(b.start), z = ms(b.usable_at);
  const bx = x(a), bw = Math.max(6, x(z) - x(a)), bh = 24, by = cy - bh / 2;
  const failed = b.state === "failed";
  const cls = "bar " + b.kind + (failed ? " failed" : "") + (b.state === "moving" ? " moving" : "") +
    (S.newIds.has("bar:" + b.id + b.kind) ? " new" : "") + (S.sel && S.sel.type === "bar" && S.sel.id === b.id + b.kind ? " sel" : "");
  const g = sv("g", { class: cls, onclick: () => select({ type: "bar", id: b.id + b.kind, lane: l.lane_id }) },
    sv("title", {}, `${b.label}: ${fmt(b.start)} → usable ${fmt(b.usable_at)}`));
  g.append(sv("rect", { class: "body", x: bx, y: by, width: bw, height: bh }));
  for (const leg of (b.legs || []).slice(1)) {
    const lx = x(ms(leg.from));
    g.append(sv("line", { class: "leg", x1: lx, x2: lx, y1: by + 4, y2: by + bh - 4 }));
  }
  for (const leg of b.legs || []) {
    const lx0 = x(ms(leg.from)), lx1 = x(ms(leg.to));
    if (lx1 - lx0 > leg.label.length * 5.6 + 6) g.append(sv("text", { class: "leg-label", x: (lx0 + lx1) / 2, y: by + bh + 12, "text-anchor": "middle" }, leg.label));
  }
  if (failed) g.append(sv("line", { class: "strike", x1: bx + 3, x2: bx + bw - 3, y1: cy, y2: cy }));
  g.append(sv("text", { class: "bar-label", x: bx + 2, y: by - 6 }, b.label));
  g.append(sv("path", { d: `M${bx + bw} ${by - 3} L${bx + bw + 5} ${by - 9} L${bx + bw - 5} ${by - 9} Z`, fill: b.kind === "ghost" || failed ? "var(--risk)" : b.kind === "hedge" ? "var(--hedge)" : "var(--supply)" }));
  return g;
}
function statusText(s) { return ({ critical: "critical", hedged: "hedged", at_risk: "at risk", covered: "covered", ok: "in stock", unassessed: "not assessed" })[s] || s; }
function statusColor(s, bg) {
  const m = { critical: "risk", at_risk: "warn", hedged: "hedge", covered: "ok", ok: "ok" }[s] || "supply";
  return `var(--${m}${bg ? "-bg" : ""})`;
}
