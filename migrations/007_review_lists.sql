-- Review lists: generated questions people flagged, with what each should have been.
-- An item snapshots the insight and survey it was added from, so the list stays
-- meaningful after the run that produced it is gone or rerun.
CREATE TABLE review_lists (
    list_id uuid PRIMARY KEY,
    name text NOT NULL CHECK (btrim(name) <> ''),
    description text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE review_items (
    item_id uuid PRIMARY KEY,
    list_id uuid NOT NULL REFERENCES review_lists ON DELETE CASCADE,
    run_id uuid NOT NULL REFERENCES generation_runs,
    insight_id text NOT NULL,
    -- NULL comments on the whole survey; otherwise one question's item_id.
    question_id text,
    comment text NOT NULL CHECK (btrim(comment) <> ''),
    should_be text,
    config_id uuid REFERENCES agent_configs,
    insight jsonb NOT NULL,
    survey jsonb NOT NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX review_items_target
    ON review_items (list_id, run_id, insight_id, coalesce(question_id, ''));
CREATE INDEX review_items_list ON review_items (list_id, created_at);

-- An improvement loop: a stronger model edits a configuration to address a review
-- list, each candidate is run on the list's insights and judged against the
-- comments, and the best is compared with a chosen configuration.
CREATE TABLE improvements (
    improvement_id uuid PRIMARY KEY,
    list_id uuid NOT NULL REFERENCES review_lists,
    spec jsonb NOT NULL,
    -- The job that runs it. No foreign key: the improvement row is written first so
    -- a runner that claims the job always finds it.
    job_id uuid NOT NULL,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    rounds jsonb NOT NULL DEFAULT '[]',
    verdict jsonb,
    best_config_id uuid REFERENCES agent_configs,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE INDEX improvements_list ON improvements (list_id, created_at);

ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('batch', 'experiment', 'improve'));
ALTER TABLE model_calls DROP CONSTRAINT model_calls_role_check;
ALTER TABLE model_calls ADD CONSTRAINT model_calls_role_check
    CHECK (role IN ('generate', 'review', 'improve', 'judge'));
