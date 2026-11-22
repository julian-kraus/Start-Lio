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

-- Append only, enforced: a row may be inserted but never updated or deleted, so a run and its injections stay a
-- faithful record of what was fed in. The trigger raises on every UPDATE or DELETE.
CREATE OR REPLACE FUNCTION append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'append only: % on %.% is not allowed', TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME;
END;
$$;

CREATE OR REPLACE TRIGGER runs_append_only BEFORE UPDATE OR DELETE ON runs
    FOR EACH ROW EXECUTE FUNCTION append_only();
CREATE OR REPLACE TRIGGER injections_append_only BEFORE UPDATE OR DELETE ON injections
    FOR EACH ROW EXECUTE FUNCTION append_only();
