CREATE TABLE IF NOT EXISTS experiments (
    experiment_id uuid PRIMARY KEY,
    spec jsonb NOT NULL,
    source text NOT NULL CHECK (source IN ('live', 'recorded')),
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'completed', 'completed_with_errors', 'failed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
ALTER TABLE generation_runs ADD COLUMN IF NOT EXISTS purpose text NOT NULL DEFAULT 'handoff'
    CHECK (purpose IN ('handoff', 'experiment'));
ALTER TABLE generation_runs ADD COLUMN IF NOT EXISTS experiment_id uuid REFERENCES experiments;
CREATE INDEX IF NOT EXISTS generation_runs_experiment_idx ON generation_runs(experiment_id);
CREATE TABLE IF NOT EXISTS quality_assessments (
    assessment_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id uuid NOT NULL REFERENCES experiments,
    assessment jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
