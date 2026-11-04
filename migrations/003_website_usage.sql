CREATE TABLE website_policy (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
    enabled boolean NOT NULL DEFAULT false,
    budget_usd numeric CHECK (budget_usd > 0 AND budget_usd < 'Infinity'::numeric),
    CHECK (NOT enabled OR budget_usd IS NOT NULL)
);
INSERT INTO website_policy(singleton) VALUES (true);
CREATE TABLE website_prices (
    price_id uuid PRIMARY KEY,
    policy_version bigint NOT NULL,
    model_id text NOT NULL,
    api text NOT NULL CHECK (api IN ('responses','converse')),
    region text NOT NULL,
    input_per_million numeric NOT NULL CHECK (input_per_million >= 0 AND input_per_million < 'Infinity'::numeric),
    output_per_million numeric NOT NULL CHECK (output_per_million >= 0 AND output_per_million < 'Infinity'::numeric),
    max_input_tokens bigint NOT NULL CHECK (max_input_tokens > 0),
    source text NOT NULL CHECK (length(source) > 0),
    bound_source text NOT NULL CHECK (length(bound_source) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(policy_version,model_id,api,region)
);
CREATE TABLE website_calls (
    call_id uuid PRIMARY KEY,
    price_id uuid NOT NULL REFERENCES website_prices,
    run_id uuid NOT NULL,
    insight_id text NOT NULL,
    role text NOT NULL CHECK (role IN ('generate','review')),
    attempt integer NOT NULL CHECK (attempt > 0),
    max_output_tokens bigint NOT NULL CHECK (max_output_tokens > 0),
    reserved_usd numeric NOT NULL CHECK (reserved_usd >= 0 AND reserved_usd < 'Infinity'::numeric),
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','settled','unknown','overrun')),
    input_tokens bigint CHECK (input_tokens >= 0),
    output_tokens bigint CHECK (output_tokens >= 0),
    estimated_usd numeric CHECK (estimated_usd >= 0 AND estimated_usd < 'Infinity'::numeric),
    error_type text,
    provider_request_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    settled_at timestamptz,
    CHECK ((state = 'pending') = (settled_at IS NULL)),
    CHECK (state <> 'settled' OR (input_tokens IS NOT NULL AND output_tokens IS NOT NULL AND estimated_usd IS NOT NULL))
);
CREATE INDEX website_calls_run_idx ON website_calls(run_id,created_at);
CREATE TABLE website_launches (
    launch_id uuid PRIMARY KEY,
    spec_hash text NOT NULL,
    policy_version bigint NOT NULL,
    experiment_id uuid NOT NULL UNIQUE REFERENCES experiments,
    created_at timestamptz NOT NULL DEFAULT now()
);
