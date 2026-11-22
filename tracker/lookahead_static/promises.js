"use strict";
(function () {
  const api = window.LookaheadWeak = window.LookaheadWeak || {};
  api.renderPromises = function (root, promises, suppliers, t) {
    const h = api.node;
    const section = h("section", "card");
    const header = h("div", "between weak-promises-head");
    const count = promises.filter(p => p.state === "waiting").length;
    header.append(h("h2", "h-sm", t("weak.promises")), h("span", "badge accent", `${t("weak.watching")} ${count}`));
    section.append(header);
    const table = h("table", "table"); const head = h("thead"); const row = h("tr");
    ["weak.who", "weak.promise", "weak.next", "weak.status", "weak.fallback"].forEach(key => row.append(h("th", "", t(key))));
    head.append(row); table.append(head);
    const body = h("tbody");
    promises.forEach(p => {
      const tr = h("tr");
      const supplier = suppliers.find(s => s.supplier_id === p.supplier_id);
      tr.append(h("td", "", supplier && supplier.name || p.supplier_id || "—"), h("td", "", p.what || "—"),
        h("td", "mono", p.next_check || "—"));
      const status = h("td");
      status.append(h("span", "badge " + (p.state === "broken" ? "risk" : p.state === "proven" ? "good" : "accent"), t("weak." + p.state)));
      tr.append(status, h("td", "", p.fallback || "—")); body.append(tr);
    });
    table.append(body); section.append(table); root.append(section);
  };
}());
