"use strict";
/* The shell calls renderWeakView with the current record state and translation function. */
(function () {
  const api = window.LookaheadWeak = window.LookaheadWeak || {};
  api.node = function (tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  };
  api.number = value => typeof value === "number" && Number.isFinite(value) ? new Intl.NumberFormat().format(value) : "";
  api.score = supplier => {
    const value = supplier.score && supplier.score.value;
    return typeof value === "number" && Number.isFinite(value) ? Math.round(value * 100) + "%" : "—";
  };
  api.tone = supplier => {
    const value = supplier.score && supplier.score.value;
    if (typeof value !== "number" || !Number.isFinite(value)) return "";
    return value >= 0.75 ? "tone-g" : value >= 0.6 ? "tone-am" : "tone-r";
  };
  function pair(title, description, value, label, cls) {
    const node = api.node("div", "gnode " + (cls || ""));
    const left = api.node("div", "gn-t");
    left.append(api.node("b", "", title), api.node("span", "", description));
    const right = api.node("div", "gn-m");
    right.append(api.node("b", "", value), api.node("span", "", label));
    node.append(left, right);
    return node;
  }
  function render(root, state, translate) {
    const t = translate || (key => key);
    const data = state || {};
    const suppliers = data.suppliers || [];
    const plan = data.plan || {};
    const lanes = plan.lanes || [];
    const card = api.node("section", "card");
    const heading = api.node("div", "between");
    heading.append(api.node("h2", "h-sm", t("weak.title")));
    const all = api.node("button", "btn btn-text sup-all", t("weak.all"));
    all.type = "button";
    heading.append(all);
    card.append(heading);
    const heads = api.node("div", "ghead");
    ["weak.supplier", "weak.part", "weak.product"].forEach(key => heads.append(api.node("span", "", t(key))));
    card.append(heads);
    const graph = api.node("div", "graph");
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.id = "wires"; svg.setAttribute("aria-hidden", "true"); graph.append(svg);
    const cols = [api.node("div", "gcol"), api.node("div", "gcol"), api.node("div", "gcol")];
    suppliers.forEach(s => {
      const n = pair(s.name || s.supplier_id, s.note || s.persona || "", api.score(s), t("weak.reliability"), "");
      n.dataset.supplierId = s.supplier_id;
      n.querySelector(".gn-m b").className = api.tone(s);
      n.addEventListener("click", () => api.openSuppliers(root, suppliers, t, s.supplier_id));
      cols[0].append(n);
    });
    lanes.forEach(lane => {
      const critical = lane.status === "critical" || lane.status === "at_risk";
      const gap = typeof lane.gap === "number" && lane.gap > 0 ? "−" + api.number(lane.gap) : api.number(lane.gap);
      const n = pair(lane.name || lane.lane_id, lane.reason || "", gap,
        t(lane.substitute ? "weak.substitute" : "weak.gap"), critical ? "crit" : "");
      n.dataset.laneId = lane.lane_id;
      n.addEventListener("click", () => api.openSuppliers(root, suppliers, t, lane.supplier_id));
      cols[1].append(n);
    });
    const coverage = plan.coverage;
    if (coverage) {
      const n = api.node("div", "gnode prod" + (coverage.covered < coverage.needed ? " crit" : ""));
      n.dataset.productId = coverage.product_id || coverage.edition || "";
      const info = api.node("div", "gn-t");
      info.append(api.node("b", "", coverage.name || coverage.unit), api.node("span", "", `${api.number(coverage.covered)} / ${api.number(coverage.needed)} ${t("weak.covered")}`));
      const bar = api.node("div", "gbar" + (coverage.covered >= coverage.needed ? " ok" : ""));
      const fill = api.node("i"); fill.style.width = (coverage.needed > 0 ? Math.max(0, Math.min(100, coverage.covered / coverage.needed * 100)) : 0) + "%";
      bar.append(fill); n.append(info, bar); cols[2].append(n);
    }
    cols.forEach(col => graph.append(col)); card.append(graph); root.append(card);
    all.addEventListener("click", () => api.openSuppliers(root, suppliers, t));
    if (api.renderPromises) api.renderPromises(root, data.promises || [], suppliers, t);
    if (api.drawWires) requestAnimationFrame(() => api.drawWires(graph, suppliers, lanes));
  }
  api.render = render;
  window.LOOKAHEAD_VIEWS = window.LOOKAHEAD_VIEWS || {};
  window.LOOKAHEAD_VIEWS.weak = render;
}());
