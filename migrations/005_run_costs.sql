-- Prices are append-only versions. Requests retain their original price reference.
CREATE TABLE model_prices (
    price_id uuid PRIMARY KEY,
    version bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    model_id text NOT NULL,
    api text NOT NULL CHECK (api IN ('responses','converse')),
    region text NOT NULL,
    pricing jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX model_prices_lookup ON model_prices(model_id,api,region,version DESC);
CREATE TABLE run_costs (
    run_id uuid PRIMARY KEY REFERENCES generation_runs,
    budget_usd numeric CHECK (budget_usd > 0 AND budget_usd < 'Infinity'::numeric),
    stop_reason text
);
CREATE TABLE model_calls (
    call_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES run_costs,
    insight_id text NOT NULL,
    role text NOT NULL CHECK (role IN ('generate','review')),
    attempt integer NOT NULL CHECK (attempt > 0),
    model_id text NOT NULL,
    api text NOT NULL,
    region text NOT NULL,
    price_id uuid REFERENCES model_prices,
    reserved_usd numeric NOT NULL CHECK (reserved_usd >= 0),
    max_output_tokens integer NOT NULL,
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','complete','unknown','overrun','unpriced')),
    input_tokens bigint,
    output_tokens bigint,
    estimated_usd numeric,
    usage jsonb,
    provider_request_id text,
    error_type text,
    created_at timestamptz NOT NULL DEFAULT now(),
    settled_at timestamptz
);
CREATE INDEX model_calls_run ON model_calls(run_id,created_at);
-- Verified 2026-11-04 against the AWS model card. Standard Mantle in-region,
-- commercial us-east-1; includes the 10% regional premium and long-context tier.
INSERT INTO model_prices(price_id,model_id,api,region,pricing) VALUES
('2e69eca0-1a77-4f49-951f-55ec6309f101','openai.gpt-6-luna','responses','us-east-1',
'{"input_per_million":"0.11","output_per_million":"0.55","cache_read_per_million":"0.011","cache_write_per_million":"0.1375","long_context_threshold":272000,"long_input_per_million":"0.22","long_output_per_million":"0.825","long_cache_read_per_million":"0.022","long_cache_write_per_million":"0.275","max_input_tokens":1050000,"source":"https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-luna.html","checked_on":"2026-11-04","notes":"Standard Mantle in-region. Text and reported cache usage only; output includes reasoning."}'),
('2e69eca0-1a77-4f49-951f-55ec6309f102','deepseek.v3.2','converse','us-east-1',
'{"input_per_million":"0.62","output_per_million":"1.85","max_input_tokens":167936,"source":"https://aws.amazon.com/bedrock/pricing/","checked_on":"2026-11-04","notes":"Standard on-demand text in us-east-1; no batch discount. Context bound (164K rounded up to 164*1024): https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-deepseek-deepseek-v3-2.html"}');

CREATE FUNCTION preserve_model_price() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Model price versions are immutable; insert a new version';
END;
$$;
CREATE TRIGGER model_prices_immutable BEFORE UPDATE OR DELETE ON model_prices
    FOR EACH ROW EXECUTE FUNCTION preserve_model_price();
