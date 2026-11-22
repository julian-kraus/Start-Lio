"use strict";
// Agent rules (#v=rules). The draft lives here, so a live update of the page does not lose what is being edited.
(() => {
  const SWITCHES = ["quotes_and_holds", "check_ins", "reminders_and_calls", "skip_hedge_with_substitute"];
  const CHANNELS = ["email", "teams", "sms"], WINDOWS = [6, 12, 24];
  let base = null, draft = null, signers = null, note = null, error = false, saving = false;
  let seenId = null, page = null, focused = null, painted = null;

  const blank = () => ({
    autonomy: { ...Object.fromEntries(SWITCHES.map((key) => [key, false])), binding_orders: false },
    hedge: { min_share_pct: null, window_months: null, size_percentile: null, unit_loss: { mode: "refund", value_eur: null } },
    escalation: { vacant_to_next_level: false, remind_hours_before: null, digest_hours_before: null, channels: [] },
  });
  const pick = (rules) => structuredClone({ autonomy: rules.autonomy, hedge: rules.hedge, escalation: rules.escalation });
  const original = () => (base ? pick(base) : blank());
  const label = (key) => t("rules." + key);
  const changed = () => draft !== null && JSON.stringify(draft) !== JSON.stringify(original());
  const invalid = () => !!page && !!page.querySelector('[aria-invalid="true"]');

  function status() {
    if (!page) return;
    const text = note || (draft === null ? "loading" : base || changed() ? null : "none");
    const line = page.querySelector(".rules-status");
    line.textContent = invalid() ? label("invalid") : text ? label(text) : "";
    line.title = error && typeof error === "string" ? error : "";
    page.querySelector(".rules-save").disabled = saving || !changed() || invalid();
  }
  function edited() { note = null; error = false; status(); }
  function item(key, control, locked) {
    return el("li", { class: locked ? "locked" : null },
      el("div", null, el("b", null, label(key)), el("p", null, label(key + ".note"))), control);
  }
  function toggle(group, key, locked) {
    const on = draft !== null && draft[group][key] === true;
    const node = el("button", { class: "sw" + (on ? " on" : ""), type: "button", role: "switch", "aria-checked": String(on),
      "aria-label": label(key), "data-field": key, disabled: locked || draft === null }, el("i"));
    node.addEventListener("click", () => {
      draft[group][key] = !draft[group][key];
      node.classList.toggle("on", draft[group][key]);
      node.setAttribute("aria-checked", String(draft[group][key]));
      edited();
    });
    return item(key, node, locked);
  }
  function numberInput(key, owner, field, limits, required) {
    const node = el("input", { type: "number", step: "any", ...limits, required, "data-field": key,
      "aria-label": label(key), disabled: draft === null });
    const value = draft === null ? null : owner()[field];
    node.value = value === null ? "" : value;
    const check = () => node.setAttribute("aria-invalid", String(!node.disabled && !node.checkValidity()));
    node.addEventListener("input", () => {
      owner()[field] = node.value === "" ? null : Number(node.value);
      check(); edited();
    });
    check();
    return node;
  }
  function numberRow(group, key, unit, limits) {
    return item(key, el("label", { class: "fld" }, numberInput(key, () => draft[group], key, limits), el("span", null, unit)));
  }
  function choice(key, value, options, change) {
    const node = el("select", { "data-field": key, "aria-label": label(key), disabled: draft === null },
      options.map(([id, text]) => el("option", { value: id }, text)));
    node.value = value;
    node.addEventListener("change", () => { change(node.value); edited(); });
    return node;
  }
  function windowRow() {
    const value = draft && draft.hedge.window_months;
    const options = WINDOWS.map((months) => [String(months), label("window." + months)]);
    if (!value) options.unshift(["", label("not_set")]);
    return item("window_months", el("label", { class: "fld" }, choice("window_months", value ? String(value) : "", options,
      (next) => { draft.hedge.window_months = next ? Number(next) : null; })));
  }
  function lossRow() {
    const loss = draft ? draft.hedge.unit_loss : { mode: "refund" };
    const box = el("label", { class: "fld" });
    box.append(choice("unit_loss", loss.mode, [["refund", label("refund")], ["custom", label("custom")]], (mode) => {
      draft.hedge.unit_loss = { mode, value_eur: null };
      paint();
      if (mode === "custom") page.querySelector('[data-field="unit_loss_value"]').focus();
    }));
    if (draft && loss.mode === "custom") {
      const input = numberInput("unit_loss", () => draft.hedge.unit_loss, "value_eur", { min: "0" }, true);
      input.dataset.field = "unit_loss_value";
      box.append(input, el("span", null, "€"));
    }
    return item("unit_loss", box);
  }
  function channelRow() {
    return item("channels", el("div", { class: "nchips" }, CHANNELS.map((id) => {
      const on = draft !== null && draft.escalation.channels.includes(id);
      const node = el("button", { class: "nch" + (on ? " on" : ""), type: "button", "aria-pressed": String(on),
        "data-field": id, disabled: draft === null }, label("channel." + id));
      node.addEventListener("click", () => {
        const now = new Set(draft.escalation.channels);
        if (now.has(id)) now.delete(id); else now.add(id);
        draft.escalation.channels = CHANNELS.filter((c) => now.has(c));
        node.classList.toggle("on", now.has(id));
        node.setAttribute("aria-pressed", String(now.has(id)));
        edited();
      });
      return node;
    })));
  }
  function money(limit) {
    const amount = limit.value.toLocaleString("en-US");
    const text = limit.currency === "EUR" ? "€" + amount : [limit.currency, amount].filter(Boolean).join(" ");
    return ({ "<": "<", "<=": "≤", ">": ">", ">=": "≥" })[limit.op] + " " + text;
  }
  function seatLine(seat) {
    const scope = seat.scope ? el("span", { class: "xs subtle" }, " " + seat.scope) : null;
    const who = seat.state === "held" ? seat.name
      : el("span", { class: "badge b-am" }, el("span", { class: "dot" }), label("seat." + seat.state));
    return el("div", { title: seat.fact_ids.join(", ") }, who, scope);
  }
  function signerCard() {
    const name = signers && signers.available ? signers.policy.name : "";
    const title = label("signers") + (name ? (S.language === "zh" ? `（${name}）` : ` (${name})`) : "");
    const card = el("section", { class: "card" }, el("h3", { class: "h-xs" }, title));
    if (!signers) card.append(el("p", { class: "xs subtle" }, label("loading")));
    else if (!signers.available) card.append(el("p", { class: "xs subtle" }, label("signers." + signers.reason)));
    else {
      const rows = signers.rows.map((row) => el("tr", null,
        el("td", null, row.role, row.with.length ? el("div", { class: "xs subtle" }, label("jointly") + " " + row.with.join(", ")) : null),
        el("td", { class: "r", title: row.limit.fact_ids.join(", ") }, money(row.limit)),
        el("td", null, row.seats.length ? row.seats.map(seatLine) : "–")));
      card.append(el("table", null,
        el("thead", null, el("tr", null, el("th", null, label("role")), el("th", { class: "r" }, label("limit")), el("th", null, label("currently")))),
        el("tbody", null, rows)));
      if (signers.problems.length) card.append(el("p", { class: "xs subtle", title: signers.problems.join("; ") }, label("signers.problems")));
    }
    return card;
  }
  function card(title, ...content) {
    return el("section", { class: "card" }, el("h3", { class: "h-xs" }, label(title)), content);
  }
  const shown = () => JSON.stringify([S.language, draft === null, base, signers]);
  // a live update rebuilds the form only when what it shows changed: typing, focus and an open select survive
  function sync() {
    if (shown() !== painted) return paint();
    const target = document.activeElement === document.body && focused && page.querySelector(`[data-field="${focused}"]`);
    if (target) target.focus({ preventScroll: true });
    status();
  }
  function paint() {
    if (!page) return;
    painted = shown();
    const active = document.activeElement;
    const field = (page.contains(active) && active.dataset.field) || (active === document.body && focused);
    page.replaceChildren(
      el("section", { class: "card" }, el("div", { class: "between", style: "gap:16px;flex-wrap:wrap" },
        el("div", null, el("h2", { class: "h-sm" }, label("title"))),
        el("div", { class: "row", style: "gap:10px" }, el("span", { class: "xs subtle rules-status", role: "status" }),
          el("button", { class: "btn btn-primary rules-save", type: "button", onclick: save }, label("save"))))),
      el("div", { class: "rgrid" },
        card("autonomy", el("ul", { class: "rlist" }, SWITCHES.map((key) => toggle("autonomy", key)), toggle("autonomy", "binding_orders", true))),
        card("hedge", el("div", { class: "formula" }, label("formula")), el("ul", { class: "rlist" },
          numberRow("hedge", "min_share_pct", "%", { min: "0", max: "100" }), windowRow(),
          numberRow("hedge", "size_percentile", "%", { min: "50", max: "100" }), lossRow())),
        signerCard(),
        card("escalation", el("ul", { class: "rlist" }, toggle("escalation", "vacant_to_next_level"),
          numberRow("escalation", "remind_hours_before", label("hours"), { min: "0" }),
          numberRow("escalation", "digest_hours_before", label("hours"), { min: "0" }), channelRow()))));
    const target = field && page.querySelector(`[data-field="${field}"]`);
    if (target) target.focus({ preventScroll: true });
    status();
  }
  async function save() {
    if (saving || !changed() || invalid()) return;
    saving = true; note = null; status();
    const sent = structuredClone(draft);
    try {
      const result = await api("/api/lookahead/rules", { ...sent, based_on: base ? base.id : null });
      base = result.rules; note = changed() ? null : "saved"; error = false;
    } catch (exc) { note = "failed"; error = exc.message; toast(exc.message, true); }
    saving = false;
    status();
  }
  async function refresh() {
    try {
      const { rules } = await api("/api/lookahead/rules");
      if ((rules && rules.id) !== (base && base.id)) {
        if (changed()) note = "changed";
        else { base = rules; draft = original(); note = null; }
      }
      if (draft === null) draft = original();
      if (note === "load_failed") note = null;
    } catch (exc) { note = "load_failed"; }
    sync();
    try { signers = await api("/api/lookahead/rules/signers"); } catch (exc) { signers = { available: false, reason: "failed" }; }
    sync();
  }
  document.addEventListener("focusin", (event) => { focused = event.target.dataset.field || null; });
  document.addEventListener("pointerdown", () => { focused = null; });
  window.LOOKAHEAD_VIEWS = window.LOOKAHEAD_VIEWS || {};
  window.LOOKAHEAD_VIEWS.rules = (view, state) => {
    if (!page) page = el("div", { class: "rules" });
    view.append(page);
    sync();
    if (state && state.last_id !== seenId) { seenId = state.last_id; refresh(); }
  };
})();
