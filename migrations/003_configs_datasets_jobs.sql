CREATE TABLE IF NOT EXISTS agent_configs (
    config_id uuid PRIMARY KEY,
    parent_id uuid REFERENCES agent_configs,
    definition jsonb NOT NULL,
    definition_sha256 text NOT NULL UNIQUE,
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'retired')),
    note text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_configs_one_active
    ON agent_configs ((status)) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS datasets (
    dataset_id uuid PRIMARY KEY,
    name text NOT NULL,
    source_sha256 text NOT NULL UNIQUE,
    row_count integer NOT NULL CHECK (row_count >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS insights (
    dataset_id uuid NOT NULL REFERENCES datasets ON DELETE CASCADE,
    ordinal integer NOT NULL,
    insight_id text NOT NULL,
    farmer_id text,
    crop text,
    concern text,
    resolution_type text,
    quality_flag text,
    created_at timestamptz,
    body jsonb NOT NULL,
    PRIMARY KEY (dataset_id, insight_id),
    UNIQUE (dataset_id, ordinal)
);
CREATE INDEX IF NOT EXISTS insights_farmer_idx ON insights (dataset_id, farmer_id);
CREATE INDEX IF NOT EXISTS insights_crop_idx ON insights (dataset_id, crop);
CREATE INDEX IF NOT EXISTS insights_created_idx ON insights (dataset_id, created_at);

CREATE TABLE IF NOT EXISTS jobs (
    job_id uuid PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('batch', 'experiment')),
    spec jsonb NOT NULL,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'completed', 'completed_with_errors', 'failed')),
    progress jsonb NOT NULL DEFAULT '{}',
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS jobs_queue_idx ON jobs (created_at) WHERE status = 'queued';

ALTER TABLE generation_runs ADD COLUMN IF NOT EXISTS config_id uuid REFERENCES agent_configs;
ALTER TABLE generation_runs ADD COLUMN IF NOT EXISTS job_id uuid REFERENCES jobs;
CREATE INDEX IF NOT EXISTS generation_runs_job_idx ON generation_runs (job_id);
