-- Configurable, versioned source rules. Exactly one version is active; edits create new versions.

CREATE TABLE IF NOT EXISTS rule_sets (
    version        serial PRIMARY KEY,
    parent_version integer REFERENCES rule_sets(version),
    active         boolean NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     text NOT NULL,
    comment        text,
    activated_at   timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS rule_sets_one_active ON rule_sets (active) WHERE active;

CREATE TABLE IF NOT EXISTS source_rules (
    version      integer NOT NULL REFERENCES rule_sets(version) ON DELETE CASCADE,
    rule_id      text NOT NULL,
    position     integer NOT NULL,
    enabled      boolean NOT NULL DEFAULT true,
    spec         jsonb NOT NULL,   -- question, key, candidates, compare, decide, min_confidence, fallback, severity, explanation
    PRIMARY KEY (version, rule_id)
);

CREATE TABLE IF NOT EXISTS settings (
    version integer NOT NULL REFERENCES rule_sets(version) ON DELETE CASCADE,
    key     text NOT NULL,
    value   jsonb NOT NULL,
    PRIMARY KEY (version, key)
);
