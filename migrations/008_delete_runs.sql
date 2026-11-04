-- Deleting a console run removes its request records, but its spend still counts
-- against the cumulative cap.
ALTER TABLE spend_cap ADD COLUMN deleted_usd numeric NOT NULL DEFAULT 0 CHECK (deleted_usd >= 0);

-- Review comments keep their snapshot when the run they came from is deleted.
ALTER TABLE review_items ALTER COLUMN run_id DROP NOT NULL;
ALTER TABLE review_items DROP CONSTRAINT review_items_run_id_fkey;
ALTER TABLE review_items ADD CONSTRAINT review_items_run_id_fkey
    FOREIGN KEY (run_id) REFERENCES generation_runs ON DELETE SET NULL;
