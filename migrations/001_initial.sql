-- MVP bootstrap migration; run automatically by the worker under a migration lock.
-- Payloads preserve participant-pack JSON; operational keys remain ordinary columns.
CREATE SEQUENCE IF NOT EXISTS survey_number_seq MINVALUE 1 MAXVALUE 9999999999;

CREATE TABLE IF NOT EXISTS generation_runs (
    run_id uuid PRIMARY KEY,
    fingerprint text NOT NULL,
    manifest jsonb NOT NULL,
    status text NOT NULL DEFAULT 'running',
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE TABLE IF NOT EXISTS insight_results (
    run_id uuid NOT NULL REFERENCES generation_runs(run_id),
    insight_id text NOT NULL,
    farmer_id text,
    status text NOT NULL CHECK (status IN ('accepted', 'skipped', 'rejected', 'failed')),
    insight jsonb NOT NULL,
    outcome jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, insight_id)
);
CREATE INDEX IF NOT EXISTS insight_results_farmer_idx ON insight_results(farmer_id);

-- This is an outbox for the future scheduler, not a delivery queue implementation.
-- Commit the result and handoff together so a crash cannot lose an accepted survey.
CREATE TABLE IF NOT EXISTS survey_handoffs (
    survey_id text PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES generation_runs(run_id),
    insight_id text NOT NULL,
    farmer_id text NOT NULL,
    not_before timestamptz NOT NULL,
    not_after timestamptz NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    UNIQUE (run_id, insight_id),
    CHECK (not_before < not_after)
);
CREATE INDEX IF NOT EXISTS survey_handoffs_due_idx
    ON survey_handoffs(status, not_before);
