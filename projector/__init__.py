"""The record projector: the deterministic baseline for the Lookahead screen.

It reads kb (current facts, believed values, issues), the goal record and the plan clock, runs the pure
`sourcing/` models and writes Lookahead records with `tracker.lookahead.append_records`. No LLM: the agents
(the hedger, the monitor) later add or replace recommendations through the same record kinds.

Every number it writes carries its source (a fact id, a goal line or a labelled scenario input), and writing
the same kb state twice adds nothing the second time: records are keyed by content, so a run is idempotent and a
changed fact writes a new record that supersedes the old one.
"""

from .launch import content_key, project, run, write_new

__all__ = ["content_key", "project", "run", "write_new"]
