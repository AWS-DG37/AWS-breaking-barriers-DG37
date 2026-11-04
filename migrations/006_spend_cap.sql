-- One cumulative ceiling across every run the console queues. Command-line runs are
-- outside it. A NULL budget refuses console runs until an operator sets a cap.
CREATE TABLE spend_cap (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    version bigint NOT NULL DEFAULT 0,
    budget_usd numeric CHECK (budget_usd > 0 AND budget_usd < 'Infinity'::numeric)
);
INSERT INTO spend_cap DEFAULT VALUES;
ALTER TABLE run_costs ADD COLUMN capped boolean NOT NULL DEFAULT false;
