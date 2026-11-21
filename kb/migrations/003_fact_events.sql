-- Hand-off surface for other layers (agents, UI): current facts as a view, and one event per document whose facts
-- changed. Consumers keep their own cursor on fact_events.id and LISTEN on channel kb_facts for a wake-up.

CREATE TABLE IF NOT EXISTS fact_events (
    id              bigserial PRIMARY KEY,
    at              timestamptz NOT NULL DEFAULT now(),
    path            text NOT NULL,
    sha256          text,               -- null when the file was removed
    previous_sha256 text[] NOT NULL DEFAULT '{}',
    change          text NOT NULL CHECK (change IN ('facts_added', 'facts_replaced', 'facts_removed', 'no_facts')),
    status          text,               -- document status: parsed, loaded, queued, needs_schema_map, ...
    n_facts         integer NOT NULL DEFAULT 0,
    fact_types      text[] NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS fact_events_path ON fact_events (path);

CREATE OR REPLACE VIEW current_facts AS
SELECT f.fact_id, f.source_path AS path, f.source_sha256 AS sha256, d.doc_kind, f.fact_type, f.subject,
       f.attribute, f.value, f.unit, f.as_of, f.locator, f.evidence, f.extractor, f.confidence, f.note,
       f.observed_at
FROM facts f
LEFT JOIN documents d ON d.path = f.source_path AND d.sha256 = f.source_sha256
WHERE f.valid;
