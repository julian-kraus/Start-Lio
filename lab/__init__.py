"""Scenario lab: a local-only Postgres seeded from a portal snapshot, so the agents can be run through many
scenarios without the portal tracker. The lab never touches the live database (see lab.db.assert_local)."""
