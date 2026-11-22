"use strict";
// ---------- overview: the manager's page ----------
// One question first (does anything need me?), then the story in plain sentences, then what comes next.
const MAIN_TYPES = new Set(["document", "offer_in", "delay", "cancellation", "agent", "email_in", "check_in", "decision", "goal"]);
function viewOverview() {
  const st = S.state, L = st.launch || {}, cov = st.plan && st.plan.coverage;
  const v = el("div", { class: "view ov" });
  v.append(needsYou());
  // Top to bottom: what is coming (in order), Now, then what happened (newest first), so the latest news is on screen.
  const story = el("ol", { class: "story" });
  const next = upcoming();
  if (next.length) story.append(el("li", { class: "day" }, "Coming up"));
  for (const u of next) story.append(el("li", { class: "item next" + (u.risk ? " risk" : "") },
    el("div", { class: "when" }, fmt(u.at)), el("div", { class: "dotcol" }, el("span", { class: "mk" })),
    el("div", { class: "what" }, el("div", { class: "t" }, u.title), u.why ? el("div", { class: "why" }, u.why) : null)));
  story.append(el("li", { class: "nowrow" }, el("span", { class: "nowtag" }, "Now · " + fmt(st.clock.now)),
    el("span", { class: "nowtxt" }, cov ? (cov.met ? "Goal met." : cov.hedged ? "On track, a backup is in flight." : `${num(cov.needed - cov.covered)} ${cov.unit} not covered yet.`) : st.goal ? "" : "No goal set yet.")));
  const evs = st.events.filter((e) => MAIN_TYPES.has(e.type) && !(e.type === "decision" && e.decision_id && st.decisions.some((d) => d.decision_id === e.decision_id && d.status === "open")))
    .sort((a, b) => ms(b.at) - ms(a.at) || b.id - a.id);
  let day = null;
  for (const e of evs) {
    const dname = fmt(e.at, { hour: undefined, minute: undefined, day: "numeric", month: "short" });
    if (dname !== day) { day = dname; story.append(el("li", { class: "day" }, dname)); }
    story.append(storyItem(e));
  }
  v.append(el("h2", { class: "vt" }, "Timeline ", el("small", {}, "the main events in plain words · click one for what the agent did and why")));
  v.append(el("div", { class: "card storycard" }, story));
  return v;
}
function needsYou() {
  const st = S.state, d = openDecisions()[0];
  if (!st.goal && st.launch && (st.launch.goal_questions || []).length) {
    return el("div", { class: "needs card" }, el("div", { class: "kicker" }, "Needs you"),
      el("h2", {}, "Set the goal for this launch"),
      el("p", { class: "rec" }, "Lookahead prices every risk against your goal. Four answers, then it watches the launch and only comes back when you have to decide."),
      el("div", { class: "acts" }, el("button", { class: "btn primary big", onclick: openGoal }, "Start goal interview")));
  }
  if (!d) {
    const cov = st.plan && st.plan.coverage;
    const ok = !cov || cov.met || cov.hedged;
    const nextP = st.promises.filter((p) => p.state === "waiting").sort((a, b) => ms(a.next_check || a.due) - ms(b.next_check || b.due))[0];
    return el("div", { class: "needs card calm" + (cov && cov.met ? " met" : "") },
      el("div", { class: "kicker" }, cov && cov.met ? "Goal met" : ok ? "Nothing needs you" : "Watching"),
      el("h2", {}, cov && cov.met ? `${num(cov.covered)} of ${num(cov.needed)} ${cov.unit} covered` : ok ? "The launch is on track" : (st.plan && st.plan.summary) || "Working on it"),
      el("p", { class: "rec" }, nextP ? `Next check: ${fmt(nextP.next_check || nextP.due)}, ${(supplier(nextP.supplier_id) || {}).name || ""}: ${nextP.what}. The agent chases if there is no proof.`
        : "The agent keeps watching and will ask you if a decision exceeds your limits."));
  }
  const signer = d.signer || {};
  const err = el("div", { class: "err" });
  const ev = st.events.find((e) => e.decision_id === d.decision_id);
  const cause = [...st.events].reverse().find((e) => e.severity === "risk" && e.impact && e.lane === d.lane && e.type !== "decision");
  return el("div", { class: "needs card hot" },
    el("div", { class: "kicker" }, "Needs your decision"),
    el("h2", {}, d.title),
    cause ? el("p", { class: "cause" }, el("b", {}, cause.title + ". "), (cause.detail && cause.detail.happened) || "") : null,
    el("p", { class: "rec" }, d.recommendation || ""),
    earlyLine(d),
    el("div", { class: "nums" },
      el("div", { class: "prot" }, el("b", {}, d.impact && d.impact.eur ? eur(d.impact.eur) : "–"), el("span", {}, d.impact ? d.impact.text || "protected" : "")),
      el("div", {}, el("b", {}, d.cost && d.cost.eur != null ? eur(d.cost.eur) : "–"), el("span", {}, d.cost ? d.cost.text || "cost" : ""))),
    el("div", { class: "acts" }, acceptButton(d, err),
      ev ? el("button", { class: "btn", onclick: () => select({ type: "event", id: ev.id }) }, "Why this?") : null,
      el("span", { class: "signer-line" }, signer.name ? `Signs: ${signer.name}, ${signer.role}` : "", signer.rule ? el("span", { class: "src" }, signer.rule) : null)),
    err);
}
// The counterfactual in one sentence: when the option would arrive if started now, versus when it was really started.
function earlyLine(d) {
  const l = lanes().find((x) => x.lane_id === d.lane);
  if (!l) return null;
  const ghost = (l.bars || []).find((b) => b.kind === "ghost");
  const live = (l.bars || []).find((b) => b.kind !== "ghost" && b.state !== "failed" && b.supplier_id === d.supplier_id);
  if (!ghost || !live) return null;
  const late = l.needed_by && ms(ghost.usable_at) > ms(l.needed_by);
  return el("div", { class: "early" },
    el("span", { class: "g" }, `Started now: usable ${hm(ghost.usable_at)}${late ? `, after the ${hm(l.needed_by)} needed-by` : ""}.`),
    el("span", { class: "k" }, ` It was started ${fmt(live.start)}, so it is usable ${hm(live.usable_at)}.`));
}
function storyItem(e) {
  const dt = e.detail || {};
  const did = (dt.actions || []).filter((a) => a.type !== "plan" && a.type !== "score").map((a) => a.title);
  const dec = e.decision_id && S.state.decisions.find((d) => d.decision_id === e.decision_id && d.status !== "open");
  if (dec) {  // a decision in the past reads as what was decided, not as an open alarm
    e = { ...e, severity: "ok", impact: null, title: `${dec.title}: ${dec.status} by ${dec.decided_by || "?"}` };
  }
  const tone = e.severity === "risk" ? "risk" : e.severity === "ok" ? "ok" : e.type === "agent" ? "agent" : "";
  const isSel = S.sel && S.sel.type === "event" && S.sel.id === e.id;
  return el("li", { class: "item " + tone + (S.newIds.has(e.id) ? " new" : "") + (isSel ? " sel" : ""), onclick: () => select({ type: "event", id: e.id }) },
    el("div", { class: "when" }, hm(e.at)),
    el("div", { class: "dotcol" }, el("span", { class: "mk" })),
    el("div", { class: "what" },
      el("div", { class: "t" }, e.title, e.impact && e.impact.eur && e.severity === "risk" ? el("span", { class: "amt" }, eur(e.impact.eur) + " at stake") : null),
      dt.happened ? el("div", { class: "why" }, dt.happened) : null,
      did.length ? el("div", { class: "did" }, el("b", {}, e.type === "agent" ? "The agent: " : "Then: "), did.join(" · ")) : null));
}
function upcoming() {
  const st = S.state, L = st.launch || {}, n = nowMs(), out = [];
  for (const p of st.promises) if (p.state === "waiting" && ms(p.next_check || p.due) > n)
    out.push({ at: p.next_check || p.due, title: `Check: ${(supplier(p.supplier_id) || {}).name || p.supplier_id}, ${p.what}`, why: `Proof expected: ${p.proof || "confirmation"}. If it is missing: ${p.fallback || "the agent chases"}.` });
  for (const l of lanes()) for (const b of l.bars || []) if (b.kind !== "ghost" && b.state !== "failed" && ms(b.usable_at) > n)
    out.push({ at: b.usable_at, title: `${b.label} usable at Hamburg`, why: l.needed_by ? `Needed by ${hm(l.needed_by)} for ${l.name.toLowerCase()}.` : "" });
  for (const l of lanes()) if (l.needed_by && ms(l.needed_by) > n && ["critical", "hedged"].includes(l.status))
    out.push({ at: l.needed_by, title: `Last moment for ${l.name.toLowerCase()} to arrive`, why: l.needed_by_why || "", risk: l.status === "critical" });
  if (L.deadline && ms(L.deadline) > n) out.push({ at: L.deadline, title: `Launch: trucks leave ${L.site || ""}`.trim(), why: "" });
  return out.sort((a, b) => ms(a.at) - ms(b.at));
}
