-- Core kb tables. Unqualified names: the migration runner sets search_path to the kb schema.

CREATE TABLE IF NOT EXISTS documents (
    path        text NOT NULL,
    sha256      text NOT NULL,
    container   text,
    doc_kind    text,
    status      text NOT NULL,          -- parsed | partial | loaded | queued | needs_schema_map | failed | removed
    extractor   text,
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
    active      boolean NOT NULL DEFAULT true,
    first_seen  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (path, sha256)
);
CREATE INDEX IF NOT EXISTS documents_active ON documents (path) WHERE active;

CREATE TABLE IF NOT EXISTS facts (
    fact_id       text PRIMARY KEY,
    fact_type     text NOT NULL,
    subject       jsonb NOT NULL,
    subject_raw   jsonb,
    attribute     text NOT NULL,
    value         jsonb,
    unit          text,
    as_of         text,
    locator       jsonb,
    evidence      text,
    extractor     text NOT NULL,
    confidence    real NOT NULL,
    checks        jsonb,
    note          text,
    source_path   text NOT NULL,
    source_sha256 text NOT NULL,
    valid         boolean NOT NULL DEFAULT true,
    observed_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS facts_type_attr ON facts (fact_type, attribute) WHERE valid;
CREATE INDEX IF NOT EXISTS facts_subject ON facts USING gin (subject);
CREATE INDEX IF NOT EXISTS facts_source_sha ON facts (source_sha256);
CREATE INDEX IF NOT EXISTS facts_source_path ON facts (source_path);

-- Registry: open vocabulary loaded from structured/contract, extended at runtime.
CREATE TABLE IF NOT EXISTS fact_types (
    name        text PRIMARY KEY,
    description text,
    attributes  jsonb,
    origin      text NOT NULL,
    first_seen  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS doc_kinds (
    name        text PRIMARY KEY,
    description text NOT NULL,
    origin      text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS schema_maps (
    signature  text PRIMARY KEY,
    format     text,
    sheet      text,
    mapping    jsonb NOT NULL,
    origin     text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS aliases (
    alias_kind   text NOT NULL,
    alias        text NOT NULL,
    canonical_id text NOT NULL,
    source       text,
    confidence   real NOT NULL DEFAULT 1,
    PRIMARY KEY (alias_kind, alias, canonical_id)
);

CREATE TABLE IF NOT EXISTS extraction_queue (
    path        text NOT NULL,
    sha256      text NOT NULL,
    container   text,
    state       text NOT NULL DEFAULT 'pending',   -- pending | done | failed | superseded
    attempts    integer NOT NULL DEFAULT 0,
    last_error  text,
    enqueued_at timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (path, sha256)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id           bigserial PRIMARY KEY,
    trigger          text NOT NULL,
    detail           jsonb NOT NULL DEFAULT '{}'::jsonb,
    rule_set_version integer,
    preview          boolean NOT NULL DEFAULT false,
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    n_issues         integer,
    n_new            integer,
    n_changed        integer,
    n_resolved       integer
);

CREATE TABLE IF NOT EXISTS believed (
    rule_id          text NOT NULL,
    key              jsonb NOT NULL,
    rule_set_version integer NOT NULL,
    value            jsonb,
    winner_label     text,
    winning_fact_ids text[] NOT NULL DEFAULT '{}',
    losing_fact_ids  text[] NOT NULL DEFAULT '{}',
    contradiction    boolean NOT NULL DEFAULT false,
    explanation      text,
    run_id           bigint REFERENCES runs(run_id),
    PRIMARY KEY (rule_id, key)
);

CREATE TABLE IF NOT EXISTS issues (
    issue_key        text PRIMARY KEY,
    kind             text NOT NULL,
    severity         text NOT NULL,
    state            text NOT NULL,       -- open | resolved
    title            text NOT NULL,
    payload          jsonb NOT NULL,
    payload_hash     text NOT NULL,
    fact_ids         text[] NOT NULL DEFAULT '{}',
    rule_set_version integer,
    first_seen       timestamptz NOT NULL DEFAULT now(),
    last_changed     timestamptz NOT NULL DEFAULT now(),
    resolved_at      timestamptz,
    run_id           bigint REFERENCES runs(run_id)
);
CREATE INDEX IF NOT EXISTS issues_open ON issues (severity, kind) WHERE state = 'open';

CREATE TABLE IF NOT EXISTS issue_events (
    id        bigserial PRIMARY KEY,
    at        timestamptz NOT NULL DEFAULT now(),
    run_id    bigint REFERENCES runs(run_id),
    issue_key text NOT NULL,
    change    text NOT NULL CHECK (change IN ('new', 'changed', 'resolved', 'reopened')),
    payload   jsonb
);
CREATE INDEX IF NOT EXISTS issue_events_key ON issue_events (issue_key);
