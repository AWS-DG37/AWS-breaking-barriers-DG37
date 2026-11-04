-- Named versions are the runnable ones people pick; unnamed drafts are test scratch.
ALTER TABLE agent_configs ADD COLUMN IF NOT EXISTS name text;
ALTER TABLE agent_configs DROP CONSTRAINT IF EXISTS agent_configs_status_check;
ALTER TABLE agent_configs ADD CONSTRAINT agent_configs_status_check
    CHECK (status IN ('draft', 'saved', 'active', 'retired'));
UPDATE agent_configs SET name = 'Baseline' WHERE name IS NULL AND status IN ('active', 'retired');
ALTER TABLE agent_configs ADD CONSTRAINT agent_configs_named_unless_draft
    CHECK (status = 'draft' OR name IS NOT NULL);
