-- English versions of saved surveys, for the console phone's language switch. One row per
-- distinct survey (survey_sha256 of its language and items), so every run that shows the
-- same survey shares one translation. A translation is model output for reading in the
-- console; it is never sent to farmers. run_id and model_id record where it came from and
-- are not foreign keys, so deleting a run keeps the translation.
CREATE TABLE survey_translations (
    survey_sha256 text PRIMARY KEY CHECK (survey_sha256 ~ '^[0-9a-f]{64}$'),
    language text NOT NULL,
    items jsonb NOT NULL,
    run_id uuid NOT NULL,
    model_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- The translation request is charged to the run whose survey it translates.
ALTER TABLE model_calls DROP CONSTRAINT model_calls_role_check;
ALTER TABLE model_calls ADD CONSTRAINT model_calls_role_check
    CHECK (role IN ('generate', 'review', 'improve', 'judge', 'translate'));
