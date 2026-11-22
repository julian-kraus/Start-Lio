"""LLM agent roles for Lookahead.

The roles (goal interview, hedger, promise broker, monitor) run on top of three building blocks that live here:
- `llm`: the provider adapter (Anthropic for live, a replay adapter for tests) and the tool-use loop that drives
  one role to a JSON verdict, with a validator guard that refuses any number the model did not take from a tool;
- `tools`: the registry that exposes the deterministic `sourcing/` functions and `mail.tools.MailTools` to a
  role as JSON-schema tools, and the grounding guard that checks a verdict against the tool results;
- `traces`: every call (model turn and tool execution) written to `plan.agent_traces`, under one `plan.runs` row.

No role picks numbers itself: the deterministic tool result is the source of truth, and when a model will not
align with it the tool result wins and the trace records the failure.
"""
