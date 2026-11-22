-- The lab's own bookkeeping, next to the kb, mail and plan schemas it drives. Append only: a run and its
-- injections are written once and never updated, so a scenario is a faithful record of what was fed in.

CREATE TABLE IF NOT EXISTS runs (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scenario      text        NOT NULL,                       -- the scenario this run plays
    started       timestamptz NOT NULL DEFAULT now(),
    seed_snapshot text                                        -- the lab snapshot this run was restored from, if any
);

CREATE TABLE IF NOT EXISTS injections (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run              bigint      NOT NULL REFERENCES runs (id),
    at_scenario_time timestamptz,                             -- where the scenario clock stood when it was injected
    event_type       text        NOT NULL,                    -- the catalog event type (lab.catalog, issue #183)
    parameters       jsonb       NOT NULL DEFAULT '{}'::jsonb, -- the event's parameters, by kb reference
    produced         jsonb       NOT NULL DEFAULT '{}'::jsonb, -- what it produced: file ids, mail ids, fact ids
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS injections_run ON injections (run, at_scenario_time);
