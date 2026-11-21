-- Registry lifecycle: what agents propose is visible at once but only counts fully after a person promotes it.
-- status: core (from the contract), auto (validated automatically, tables only), proposed, approved, rejected.

ALTER TABLE doc_kinds   ADD COLUMN IF NOT EXISTS status     text NOT NULL DEFAULT 'core';
ALTER TABLE doc_kinds   ADD COLUMN IF NOT EXISTS recognise  jsonb;
ALTER TABLE doc_kinds   ADD COLUMN IF NOT EXISTS version    integer NOT NULL DEFAULT 1;
ALTER TABLE doc_kinds   ADD COLUMN IF NOT EXISTS created_by text;
ALTER TABLE schema_maps ADD COLUMN IF NOT EXISTS status     text NOT NULL DEFAULT 'core';
ALTER TABLE schema_maps ADD COLUMN IF NOT EXISTS created_by text;
ALTER TABLE schema_maps ADD COLUMN IF NOT EXISTS validation jsonb;
ALTER TABLE fact_types  ADD COLUMN IF NOT EXISTS status     text NOT NULL DEFAULT 'core';
ALTER TABLE fact_types  ADD COLUMN IF NOT EXISTS alias_of   text;

UPDATE fact_types SET status = 'proposed' WHERE origin = 'proposed' AND status = 'core';

ALTER TABLE extraction_queue ADD COLUMN IF NOT EXISTS doc_kind text;
ALTER TABLE extraction_queue ADD COLUMN IF NOT EXISTS routing  jsonb;

CREATE TABLE IF NOT EXISTS promotions (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    by_user     text NOT NULL,
    object_kind text NOT NULL CHECK (object_kind IN ('doc_kind', 'schema_map', 'fact_type')),
    name        text NOT NULL,
    action      text NOT NULL CHECK (action IN ('promote', 'reject', 'alias')),
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS extraction_evals (
    id            bigserial PRIMARY KEY,
    at            timestamptz NOT NULL DEFAULT now(),
    adapter       text NOT NULL,
    doc_kind      text NOT NULL,
    files         integer NOT NULL,
    ref_facts     integer NOT NULL,
    matched       integer NOT NULL,
    agree         integer NOT NULL,
    extra_facts   integer NOT NULL,
    agreement     real,
    detail        jsonb NOT NULL DEFAULT '{}'::jsonb
);
