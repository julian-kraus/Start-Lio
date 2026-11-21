-- Core mail tables. Unqualified names: the migration runner sets search_path to the mail schema.

CREATE TABLE IF NOT EXISTS settings (
    key   text PRIMARY KEY,
    value jsonb NOT NULL
);
INSERT INTO settings (key, value) VALUES ('sending_enabled', 'true') ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS contacts (
    address      text PRIMARY KEY,
    supplier_id  text,
    display_name text,
    language     text,
    source       text NOT NULL,
    is_test      boolean NOT NULL DEFAULT false,
    send_allowed boolean NOT NULL DEFAULT true,
    added_at     timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    revoked_at   timestamptz
);
CREATE INDEX IF NOT EXISTS contacts_supplier ON contacts (supplier_id) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS threads (
    thread_id       uuid PRIMARY KEY,
    tag             text UNIQUE NOT NULL,
    supplier_id     text,
    subject         text NOT NULL,
    po_numbers      text[] NOT NULL DEFAULT '{}',
    status          text NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'waiting_reply', 'replied', 'bounced', 'closed')),
    cause           jsonb NOT NULL DEFAULT '{}'::jsonb,
    gm_thrid        bigint,
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_message_at timestamptz
);
CREATE INDEX IF NOT EXISTS threads_supplier ON threads (supplier_id, status);
CREATE INDEX IF NOT EXISTS threads_gm ON threads (gm_thrid);

CREATE TABLE IF NOT EXISTS messages (
    id           bigserial PRIMARY KEY,
    message_id   text UNIQUE NOT NULL,
    thread_id    uuid REFERENCES threads(thread_id),
    direction    text NOT NULL CHECK (direction IN ('in', 'out')),
    kind         text NOT NULL CHECK (kind IN ('reply', 'new', 'auto_reply', 'bounce', 'outbound')),
    match_method text,
    from_addr    text,
    from_name    text,
    to_addrs     text[] NOT NULL DEFAULT '{}',
    cc_addrs     text[] NOT NULL DEFAULT '{}',
    subject      text,
    in_reply_to  text,
    refs         text[] NOT NULL DEFAULT '{}',
    date_header  timestamptz,
    received_at  timestamptz NOT NULL DEFAULT now(),
    supplier_id  text,
    trust        text NOT NULL CHECK (trust IN ('supplier_contact', 'test', 'unknown_sender', 'self')),
    lang         text,
    flags        text[] NOT NULL DEFAULT '{}',
    body_text    text,
    body_en      text,
    raw          bytea NOT NULL,
    raw_sha256   text NOT NULL,
    gm_msgid     bigint,
    gm_thrid     bigint,
    folder       text,
    imap_uid     bigint
);
CREATE INDEX IF NOT EXISTS messages_thread ON messages (thread_id, received_at);
CREATE INDEX IF NOT EXISTS messages_irt ON messages (in_reply_to);
CREATE INDEX IF NOT EXISTS messages_refs ON messages USING gin (refs);
CREATE INDEX IF NOT EXISTS messages_unmatched ON messages (received_at) WHERE thread_id IS NULL AND direction = 'in';

CREATE TABLE IF NOT EXISTS attachments (
    id           bigserial PRIMARY KEY,
    message_pk   bigint NOT NULL REFERENCES messages(id),
    filename     text NOT NULL,
    content_type text,
    bytes        bigint NOT NULL,
    sha256       text NOT NULL,
    kb_path      text NOT NULL,
    UNIQUE (message_pk, sha256, filename)
);

CREATE TABLE IF NOT EXISTS drafts (
    draft_id            uuid PRIMARY KEY,
    thread_id           uuid NOT NULL REFERENCES threads(thread_id),
    reply_to_message_id text,
    supplier_id         text,
    to_addrs            text[] NOT NULL,
    cc_addrs            text[] NOT NULL DEFAULT '{}',
    subject             text NOT NULL,
    lang                text NOT NULL,
    body_local          text NOT NULL,
    body_en             text NOT NULL,
    body_back_en        text,
    po_numbers          text[] NOT NULL DEFAULT '{}',
    cause               jsonb NOT NULL DEFAULT '{}'::jsonb,
    content_sha256      text NOT NULL,
    status              text NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'sent', 'dry_run_sent', 'cancelled', 'failed')),
    created_by          text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id    uuid PRIMARY KEY,
    draft_id       uuid NOT NULL REFERENCES drafts(draft_id),
    content_sha256 text NOT NULL,
    decision       text NOT NULL CHECK (decision IN ('approved', 'rejected')),
    approver       text NOT NULL,
    approver_role  text,
    policy_ref     text,
    decided_at     timestamptz NOT NULL DEFAULT now(),
    used_at        timestamptz
);

CREATE TABLE IF NOT EXISTS sends (
    send_id       bigserial PRIMARY KEY,
    draft_id      uuid NOT NULL REFERENCES drafts(draft_id),
    approval_id   uuid NOT NULL REFERENCES approvals(approval_id),
    mode          text NOT NULL CHECK (mode IN ('dry_run', 'test_only', 'live')),
    state         text NOT NULL CHECK (state IN ('sending', 'sent', 'dry_run', 'failed', 'unknown')),
    recipients    text[] NOT NULL DEFAULT '{}',
    supplier_id   text,
    message_id    text,
    rendered      bytea,
    smtp_response text,
    error         text,
    at            timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS sends_one_per_draft ON sends (draft_id)
    WHERE state IN ('sending', 'sent', 'unknown');
CREATE INDEX IF NOT EXISTS sends_at ON sends (at);

CREATE TABLE IF NOT EXISTS audit_log (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    actor       text NOT NULL,
    action      text NOT NULL,
    object_kind text NOT NULL,
    object_id   text,
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS events (
    id      bigserial PRIMARY KEY,
    at      timestamptz NOT NULL DEFAULT now(),
    kind    text NOT NULL,
    payload jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS consumer_cursors (
    consumer      text PRIMARY KEY,
    last_event_id bigint NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sync_state (
    folder      text PRIMARY KEY,
    uidvalidity bigint NOT NULL,
    last_uid    bigint NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
