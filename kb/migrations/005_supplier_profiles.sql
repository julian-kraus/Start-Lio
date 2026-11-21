-- One row per supplier, rebuilt on every (non-preview) reconcile run from facts and open issues.
CREATE TABLE IF NOT EXISTS supplier_profiles (
    supplier_id      text PRIMARY KEY,
    profile          jsonb NOT NULL,
    rule_set_version integer,
    run_id           bigint REFERENCES runs(run_id),
    computed_at      timestamptz NOT NULL DEFAULT now()
);
