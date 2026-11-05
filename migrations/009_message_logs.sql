-- Message history. An external sender (scheduler / messaging platform, out of scope
-- here) records one row per message event and reports when it actually happened.
-- This is a log the scheduler writes into; it does not send anything itself.
--
-- Relationship to survey_handoffs: survey_id is the stable column-level identifier on
-- survey_handoffs (its primary key). handoff_id ("H-...") lives inside the handoff
-- payload JSONB, so it is not duplicated as a column here; link by survey_id and read
-- handoff_id from survey_handoffs.payload when needed. survey_id is nullable so a
-- message that is not tied to a known survey can still be logged.
--
-- Timestamps are distinct and must not be conflated:
--   sent_at      - when the external system actually sent the message (from its event).
--   delivered_at - delivery confirmation from the platform, if any.
--   failed_at    - when the send failed, if it did.
--   created_at   - when THIS log row was written here. Never a substitute for sent_at,
--                  and sent_at is never derived from the survey's calculated send window.
CREATE TABLE IF NOT EXISTS message_logs (
    id uuid PRIMARY KEY,
    farmer_id text NOT NULL,
    survey_id text REFERENCES survey_handoffs(survey_id),
    channel text NOT NULL CHECK (channel IN ('app','web','whatsapp','telegram','voice','sms')),
    message_type text NOT NULL
        CHECK (message_type IN ('initial_survey','reminder','other')),
    status text NOT NULL CHECK (status IN ('sent','delivered','failed')),
    message_content jsonb,
    external_message_id text,
    sent_at timestamptz,
    delivered_at timestamptz,
    failed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    -- A 'failed' event has no sent_at; sent/delivered events must carry one. Timestamps
    -- come from the sender's report, so this guards against conflating them with our own
    -- created_at rather than inventing a value.
    CHECK ((status = 'failed') OR (sent_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS message_logs_farmer_idx ON message_logs(farmer_id, created_at);
CREATE INDEX IF NOT EXISTS message_logs_survey_idx ON message_logs(survey_id);

-- Idempotency: a platform message id identifies one real send, so never log it twice.
-- Partial so rows without an external id (not yet assigned) are still allowed.
CREATE UNIQUE INDEX IF NOT EXISTS message_logs_external_id_key
    ON message_logs(external_message_id)
    WHERE external_message_id IS NOT NULL;
