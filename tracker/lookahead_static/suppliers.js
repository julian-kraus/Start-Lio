"use strict";
(function () {
  const api = window.LookaheadWeak = window.LookaheadWeak || {};
  api.openSuppliers = function (root, suppliers, t, selected) {
    const h = api.node;
    const existing = root.querySelector(".weak-overlay");
    if (existing) existing.remove();
    const overlay = h("div", "weak-overlay");
    const scrim = h("div", "scrim on");
    const drawer = h("aside", "ldrawer on");
    drawer.setAttribute("aria-label", t("weak.suppliers"));
    const header = h("div", "dhead");
    const title = h("div"); title.append(h("p", "h-sm", t("weak.suppliers")));
    const close = h("button", "btn btn-ghost", t("weak.close")); close.type = "button";
    const dismiss = () => overlay.remove();
    close.addEventListener("click", dismiss); scrim.addEventListener("click", dismiss);
    header.append(title, close); drawer.append(header);
    const body = h("div", "dbody supl");
    suppliers.forEach(s => {
      const article = h("article", "sup" + (s.supplier_id === selected ? " flash" : ""));
      const top = h("div", "sup-h");
      const initials = (s.name || s.supplier_id || "").split(/\s+/).filter(Boolean).slice(0, 2).map(x => x[0]).join("").toUpperCase();
      const info = h("div", "sup-n");
      const details = [s.city, s.country, s.lang ? `${t("weak.writes")} ${String(s.lang).toUpperCase()}` : ""].filter(Boolean).join(" · ");
      info.append(h("b", "", s.name || s.supplier_id), h("span", "", details));
      const score = h("div", "sup-r"); score.append(h("b", api.tone(s), api.score(s)), h("span", "", t("weak.reliability")));
      if (s.score && s.score.method) score.title = `${t("weak.source")}: ${s.score.method}`;
      top.append(h("span", "sup-av", initials), info, score); article.append(top);
      if (s.persona || s.note) article.append(h("p", "sup-d", s.persona || s.note));
      (s.flags || []).forEach(flag => article.append(h("span", "sup-tag", typeof flag === "string" ? flag : flag.label || "")));
      body.append(article);
    });
    if (!suppliers.length) body.append(h("p", "subtle", t("weak.none")));
    drawer.append(body); overlay.append(scrim, drawer); root.append(overlay); close.focus();
  };
}());
