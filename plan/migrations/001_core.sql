-- Core plan tables. Unqualified names: the migration runner sets search_path to the plan schema.
-- Every row that comes from a model run points at runs.run_id; every number carries its sources as jsonb
-- (fact ids, goal lines or labelled scenario inputs).

CREATE TABLE IF NOT EXISTS clock (
    id           smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    mode         text NOT NULL DEFAULT 'real' CHECK (mode IN ('real', 'scenario')),
    scenario_now timestamptz,
    rate         double precision NOT NULL DEFAULT 0 CHECK (rate >= 0),
    set_at       timestamptz NOT NULL DEFAULT now(),
    set_by       text,
    CHECK (mode = 'real' OR scenario_now IS NOT NULL)
);
INSERT INTO clock (id) VALUES (1) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS runs (
    run_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind        text NOT NULL,
    status      text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed')),
    scenario    boolean NOT NULL DEFAULT false,
    clock_now   timestamptz NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    input       jsonb NOT NULL DEFAULT '{}',
    output      jsonb,
    error       text
);
CREATE INDEX IF NOT EXISTS runs_kind_started ON runs (kind, started_at DESC);

CREATE TABLE IF NOT EXISTS goals (
    goal_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    product        text,
    statement      text NOT NULL,
    deadline       timestamptz,
    unit_loss_eur  numeric CHECK (unit_loss_eur >= 0),
    budget_cap_eur numeric CHECK (budget_cap_eur >= 0),
    edition        text,
    sacrifices     jsonb NOT NULL DEFAULT '[]',
    answers        jsonb NOT NULL DEFAULT '[]',
    sources        jsonb NOT NULL DEFAULT '[]',
    created_by     text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    superseded_at  timestamptz
);
CREATE INDEX IF NOT EXISTS goals_current ON goals (product) WHERE superseded_at IS NULL;

CREATE TABLE IF NOT EXISTS weak_spots (
    id          bigserial PRIMARY KEY,
    run_id      uuid NOT NULL REFERENCES runs (run_id),
    part_id     text NOT NULL,
    lane        text,
    rank        integer NOT NULL CHECK (rank >= 1),
    p_fail      double precision CHECK (p_fail BETWEEN 0 AND 1),
    exposure_eur numeric,
    reason      text NOT NULL,
    detail      jsonb NOT NULL DEFAULT '{}',
    sources     jsonb NOT NULL DEFAULT '[]',
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, part_id)
);

CREATE TABLE IF NOT EXISTS offers (
    offer_id       text PRIMARY KEY,
    supplier_id    text NOT NULL,
    part_id        text NOT NULL,
    quantity       numeric CHECK (quantity > 0),
    min_quantity   numeric CHECK (min_quantity >= 0),
    unit_price_eur numeric CHECK (unit_price_eur >= 0),
    fixed_cost_eur numeric NOT NULL DEFAULT 0 CHECK (fixed_cost_eur >= 0),
    lead_time_h    double precision CHECK (lead_time_h >= 0),
    available_at   timestamptz,
    valid_until    timestamptz,
    status         text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'requested', 'accepted', 'declined', 'expired')),
    sources        jsonb NOT NULL DEFAULT '[]',
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS offers_part ON offers (part_id, status);

CREATE TABLE IF NOT EXISTS reliability (
    supplier_id text NOT NULL,
    part_id     text NOT NULL DEFAULT '',
    computed_at timestamptz NOT NULL DEFAULT now(),
    run_id      uuid REFERENCES runs (run_id),
    value       double precision NOT NULL CHECK (value BETWEEN 0 AND 1),
    n           integer NOT NULL CHECK (n >= 0),
    prior       double precision CHECK (prior BETWEEN 0 AND 1),
    k           double precision CHECK (k >= 0),
    method      text NOT NULL,
    breakdown   jsonb NOT NULL DEFAULT '[]',
    sources     jsonb NOT NULL DEFAULT '[]',
    PRIMARY KEY (supplier_id, part_id, computed_at)
);

CREATE TABLE IF NOT EXISTS recommendations (
    recommendation_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id             uuid NOT NULL REFERENCES runs (run_id),
    kind               text NOT NULL CHECK (kind IN ('hedge', 'buffer', 'expedite', 'substitute', 'hold')),
    part_id            text NOT NULL,
    lane               text,
    supplier_id        text,
    backup_supplier_id text,
    offer_id           text REFERENCES offers (offer_id),
    quantity           numeric CHECK (quantity >= 0),
    p_fail             double precision CHECK (p_fail BETWEEN 0 AND 1),
    loss_per_copy_eur  numeric CHECK (loss_per_copy_eur >= 0),
    commitment_eur     numeric CHECK (commitment_eur >= 0),
    cost_model         jsonb NOT NULL DEFAULT '{}',
    expected_loss_eur  numeric,
    decide_by          timestamptz,
    buffer_inputs      jsonb,
    rationale          text NOT NULL,
    sources            jsonb NOT NULL DEFAULT '[]',
    status             text NOT NULL DEFAULT 'proposed'
                       CHECK (status IN ('proposed', 'decided', 'superseded', 'expired')),
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS recommendations_part ON recommendations (part_id, created_at DESC);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id       text PRIMARY KEY,
    recommendation_id uuid REFERENCES recommendations (recommendation_id),
    status            text NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open', 'accepted', 'rejected', 'expired', 'superseded')),
    title             text NOT NULL,
    signer            jsonb NOT NULL DEFAULT '{}',
    options           jsonb NOT NULL DEFAULT '[]',
    on_accept         jsonb NOT NULL DEFAULT '[]',
    decided_by        text,
    decided_at        timestamptz,
    note              text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CHECK (status IN ('open', 'expired', 'superseded') OR (decided_by IS NOT NULL AND decided_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS promises (
    promise_id   text PRIMARY KEY,
    supplier_id  text NOT NULL,
    part_id      text,
    lane         text,
    decision_id  text REFERENCES decisions (decision_id),
    what         text NOT NULL,
    quantity     numeric CHECK (quantity >= 0),
    due          timestamptz NOT NULL,
    proof        text,
    state        text NOT NULL DEFAULT 'waiting' CHECK (state IN ('waiting', 'proven', 'broken', 'cancelled')),
    next_check   timestamptz,
    fallback     text,
    proven_at    timestamptz,
    broken_at    timestamptz,
    sources      jsonb NOT NULL DEFAULT '[]',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS promises_open ON promises (next_check) WHERE state = 'waiting';

CREATE TABLE IF NOT EXISTS checkins (
    id         bigserial PRIMARY KEY,
    promise_id text NOT NULL REFERENCES promises (promise_id),
    at         timestamptz NOT NULL,
    outcome    text NOT NULL CHECK (outcome IN ('on_track', 'late', 'proven', 'broken', 'no_answer')),
    note       text,
    evidence   jsonb NOT NULL DEFAULT '[]',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS checkins_promise ON checkins (promise_id, at);

CREATE TABLE IF NOT EXISTS timeline_events (
    id         bigserial PRIMARY KEY,
    at         timestamptz NOT NULL,
    type       text NOT NULL,
    title      text NOT NULL,
    severity   text,
    lane       text,
    supplier_id text,
    payload    jsonb NOT NULL DEFAULT '{}',
    scenario   boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS timeline_events_at ON timeline_events (at);

CREATE TABLE IF NOT EXISTS agent_traces (
    id          bigserial PRIMARY KEY,
    run_id      uuid REFERENCES runs (run_id),
    role        text NOT NULL,
    step        integer NOT NULL CHECK (step >= 0),
    tool        text,
    input       jsonb NOT NULL DEFAULT '{}',
    output      jsonb,
    model       text,
    tokens_in   integer,
    tokens_out  integer,
    verdict_ok  boolean,
    error       text,
    at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_traces_run ON agent_traces (run_id, step);
