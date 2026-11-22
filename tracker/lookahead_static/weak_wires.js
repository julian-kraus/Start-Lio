"use strict";
(function () {
  const api = window.LookaheadWeak = window.LookaheadWeak || {};
  api.drawWires = function (graph, suppliers, lanes) {
    const svg = graph.querySelector("#wires");
    if (!svg) return;
    svg.replaceChildren();
    const rect = graph.getBoundingClientRect();
    svg.setAttribute("width", rect.width); svg.setAttribute("height", rect.height);
    const sourceNodes = Array.from(graph.querySelectorAll("[data-supplier-id]"));
    const partNodes = Array.from(graph.querySelectorAll("[data-lane-id]"));
    suppliers.forEach(supplier => {
      const source = sourceNodes.find(n => n.dataset.supplierId === supplier.supplier_id);
      if (!source) return;
      const parts = (supplier.parts || []).map(p => typeof p === "object" ? p.part_id || p.lane_id : p);
      lanes.filter(l => parts.includes(l.lane_id) || l.supplier_id === supplier.supplier_id).forEach(lane => {
        const target = partNodes.find(n => n.dataset.laneId === lane.lane_id);
        if (!target) return;
        const a = source.getBoundingClientRect(), b = target.getBoundingClientRect();
        const x1 = a.right - rect.left, y1 = a.top + a.height / 2 - rect.top;
        const x2 = b.left - rect.left, y2 = b.top + b.height / 2 - rect.top;
        const mid = (x1 + x2) / 2;
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", `M${x1} ${y1} C${mid} ${y1},${mid} ${y2},${x2} ${y2}`);
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", lane.status === "critical" || lane.status === "at_risk" ? "var(--red)" : "var(--s3)");
        path.setAttribute("stroke-width", "1.5"); svg.append(path);
      });
    });
    const product = graph.querySelector("[data-product-id]");
    if (product && product.dataset.productId) lanes.forEach(lane => {
      if ((lane.product_id || lane.edition) !== product.dataset.productId) return;
      const source = partNodes.find(n => n.dataset.laneId === lane.lane_id);
      if (!source) return;
      const a = source.getBoundingClientRect(), b = product.getBoundingClientRect();
      const x1 = a.right - rect.left, y1 = a.top + a.height / 2 - rect.top;
      const x2 = b.left - rect.left, y2 = b.top + b.height / 2 - rect.top;
      const mid = (x1 + x2) / 2;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", `M${x1} ${y1} C${mid} ${y1},${mid} ${y2},${x2} ${y2}`);
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", lane.status === "critical" || lane.status === "at_risk" ? "var(--red)" : "var(--s3)");
      path.setAttribute("stroke-width", "1.5"); svg.append(path);
    });
  };
}());
