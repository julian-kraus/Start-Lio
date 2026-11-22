"use strict";
// ---------- render ----------
function render() {
  if (!S.state) return;
  renderHeader();
  renderTabs();
  renderDrawer();  // first, so the stage has its final width when the timeline measures it
  const stage = $("stage");
  const keepScroll = stage.scrollTop;
  stage.replaceChildren(renderView());
  stage.scrollTop = keepScroll;
  renderOverlay();
  renderOperator();
}

function renderHeader() {
  const st = S.state, L = st.launch || {};
  $("launch-name").textContent = L.name ? `${L.name} · launch ${fmt(L.deadline)}` : "no launch loaded";
  document.title = L.name ? `Lookahead · ${L.name}` : "Lookahead";
  // goal bar
  const g = $("goal");
  const cov = st.plan && st.plan.coverage;
  if (!st.goal || !cov) {
    g.className = "goal empty";
    g.replaceChildren(
      el("div", { class: "label" }, el("strong", {}, st.goal ? "Goal set" : "No goal yet"), st.goal ? "waiting for the plan" : "Lookahead needs a goal"),
      el("div", { class: "goalbar" }, el("div", { class: "fill", style: "width:0" })),
      st.goal ? el("span") : el("button", { class: "btn", onclick: openGoal }, "Set goal ", el("kbd", {}, "G")));
  } else {
    const pct = cov.fraction;
    const state = cov.met ? "met" : cov.hedged ? "hedged" : "risk";
    const fill = el("div", { class: "fill", style: `width:${pct * 100}%` });
    g.className = "goal";
    g.replaceChildren(
      el("div", { class: "label", title: (st.goal.statement || "") }, el("strong", {}, st.goal.statement || "Goal"),
        cov.met ? "goal met" : cov.hedged ? "covered, backup in flight" : `${num(cov.needed - cov.covered)} ${cov.unit} not covered`),
      el("div", { class: "goalbar " + state }, fill),
      el("div", { class: "num" }, num(cov.covered), el("small", {}, ` / ${num(cov.needed)}`)));
    if (S.prevCover && S.prevCover.covered !== cov.covered) {
      const from = S.prevCover.fraction;
      fill.animate([{ width: from * 100 + "%" }, { width: pct * 100 + "%" }], { duration: 1000, easing: "cubic-bezier(.2,.8,.2,1)" });
    }
  }
  renderClock();
}

function renderClock() {
  const st = S.state, L = st.launch || {}, c = st.clock;
  const left = L.deadline ? ms(L.deadline) - ms(c.now) : null;
  const leftTxt = left == null ? "" : left <= 0 ? "launch passed" :
    `T−${Math.floor(left / 3600000)}:${String(Math.floor(left / 60000) % 60).padStart(2, "0")} to launch`;
  $("clock").replaceChildren(...[
    st.hidden_records ? el("span", { class: "pill", title: "Records in the log that neither an agent nor the monitor produced. Live mode does not show them." }, `${st.hidden_records} hidden`) : null,
    el("span", { class: "pill " + (c.error ? "err" : c.simulated ? "sim" : "live"), title: c.error || clockTitle() },
      c.error ? "Clock error" : c.simulated ? (st.mode === "live" ? "Live scenario" : "Simulated") : "Live"),
    el("div", { class: "clock" }, el("span", { class: "now" }, fmt(c.now)), el("span", { class: "left" }, leftTxt)),
  ].filter(Boolean));
}

function clockTitle() {
  if (S.state.mode !== "live") return "Rehearsal on a scenario clock";
  return S.state.clock.simulated ? "Live records on the shared scenario clock" : "Wall clock";
}

const VIEWS = [["overview", "Overview"], ["timeline", "Detailed timeline"], ["decisions", "Decisions"], ["promises", "Promises"], ["suppliers", "Suppliers"], ["weak", "Weak spots"]];
function renderTabs() {
  const st = S.state;
  const counts = { decisions: openDecisions().length, promises: st.promises.filter((p) => p.state === "waiting" || p.state === "chasing").length,
    weak: lanes().filter((l) => ["critical", "at_risk"].includes(l.status)).length };
  $("tabs").replaceChildren(
    ...VIEWS.map(([k, label]) => el("button", { role: "tab", "aria-selected": String(S.view === k), onclick: () => setView(k) }, label,
      counts[k] ? el("span", { class: "count" + (k !== "promises" ? " hot" : "") }, counts[k]) : null)),
    el("span", { class: "spacer" }),
    el("a", { href: "/#mail", target: "_blank", rel: "noopener" }, "Mail"),
    el("a", { href: "/", target: "_blank", rel: "noopener" }, "Portal changes"));
}
function setView(v) { S.view = v; history.replaceState(null, "", "#view-" + v); render(); }

function renderView() {
  if (S.view === "overview") return viewOverview();
  if (S.view === "decisions") return viewDecisions();
  if (S.view === "promises") return viewPromises();
  if (S.view === "suppliers") return viewSuppliers();
  if (S.view === "weak") return viewWeak();
  return viewTimeline();
}
