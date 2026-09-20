-- Migration 006: Status History, Priority Scores, Review Queue, and Settings

CREATE TABLE IF NOT EXISTS task_status_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES unified_tasks(id) ON DELETE CASCADE,
    previous_status VARCHAR(30),
    new_status VARCHAR(30) NOT NULL,
    changed_by UUID REFERENCES people(id),
    reason TEXT,
    changed_at TIMESTAMPTZ DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_status_history_task 
ON task_status_history (task_id, changed_at DESC);

CREATE TABLE IF NOT EXISTS priority_scores (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES unified_tasks(id) ON DELETE CASCADE,
    score FLOAT NOT NULL,
    breakdown JSONB NOT NULL,
    computed_at TIMESTAMPTZ DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_priority_scores_task 
ON priority_scores (task_id, computed_at DESC);

CREATE TABLE IF NOT EXISTS review_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    raw_event_id UUID REFERENCES raw_events(id),
    candidate_payload JSONB NOT NULL,
    status VARCHAR(30) DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'edited')),
    reason TEXT,
    reviewed_by UUID REFERENCES people(id),
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_review_queue_pending 
ON review_queue (created_at ASC) 
WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS dismissed_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    item_type VARCHAR(50) NOT NULL, -- 'task', 'commitment', 'risk'
    item_id VARCHAR(255) NOT NULL,
    reason TEXT,
    dismissed_at TIMESTAMPTZ DEFAULT clock_timestamp(),
    UNIQUE(user_id, item_type, item_id)
);

CREATE TABLE IF NOT EXISTS user_corrections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    target_type VARCHAR(50) NOT NULL,
    target_id VARCHAR(255) NOT NULL,
    original_data JSONB NOT NULL,
    corrected_data JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS workspace_settings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID UNIQUE NOT NULL,
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ DEFAULT clock_timestamp()
);
