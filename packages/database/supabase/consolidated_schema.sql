-- ==============================================================================
-- PERSONAL TASK BOARD: CONSOLIDATED SUPABASE SCHEMA
-- Chạy toàn bộ file này một lần duy nhất trong Supabase SQL Editor
-- ==============================================================================

-- 1. EXTENSIONS
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- 2. PEOPLE & IDENTITIES
CREATE TABLE IF NOT EXISTS people (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL,
    canonical_name VARCHAR(255) NOT NULL,
    primary_email VARCHAR(255) UNIQUE NOT NULL,
    avatar_url TEXT,
    is_current_user BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_people_workspace ON people (workspace_id);
CREATE INDEX IF NOT EXISTS idx_people_current_user ON people (workspace_id) WHERE is_current_user = TRUE;

CREATE TABLE IF NOT EXISTS source_identities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id UUID NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    tenant_id VARCHAR(100) NOT NULL,
    source_type VARCHAR(50) NOT NULL,
    external_id VARCHAR(255) NOT NULL,
    external_username VARCHAR(255),
    external_display_name VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT clock_timestamp(),
    UNIQUE(tenant_id, source_type, external_id)
);

CREATE INDEX IF NOT EXISTS idx_source_identities_lookup 
ON source_identities (tenant_id, source_type, external_id);

-- 3. CONNECTIONS & CHECKPOINTS
CREATE TABLE IF NOT EXISTS source_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    tenant_id VARCHAR(100) NOT NULL,
    source_type VARCHAR(50) NOT NULL,
    auth_type VARCHAR(30) NOT NULL DEFAULT 'oauth2',
    encrypted_credentials BYTEA NOT NULL,
    token_expires_at TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ DEFAULT clock_timestamp(),
    UNIQUE(user_id, tenant_id, source_type)
);

CREATE TABLE IF NOT EXISTS sync_checkpoints (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id UUID NOT NULL REFERENCES source_connections(id) ON DELETE CASCADE,
    sync_mode VARCHAR(30) NOT NULL, -- 'initial_backfill', 'incremental'
    page_token TEXT,
    delta_token TEXT,
    last_event_timestamp TIMESTAMPTZ,
    last_successful_sync_at TIMESTAMPTZ,
    status VARCHAR(30) DEFAULT 'healthy' CHECK (status IN ('healthy', 'syncing', 'error', 'warning')),
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ DEFAULT clock_timestamp(),
    UNIQUE(connection_id, sync_mode)
);

-- 4. RAW EVENTS (IMMUTABLE LAYER 1 INGESTION STORE)
CREATE TABLE IF NOT EXISTS raw_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id VARCHAR(100) NOT NULL,
    source_type VARCHAR(50) NOT NULL,
    external_id VARCHAR(255) NOT NULL,
    parent_external_id VARCHAR(255),
    idempotency_key VARCHAR(64) UNIQUE NOT NULL,
    
    event_timestamp TIMESTAMPTZ NOT NULL,
    author_external_id VARCHAR(255) NOT NULL,
    author_display_name VARCHAR(255),
    conversation_or_project_id VARCHAR(255) NOT NULL,
    deep_link TEXT,
    
    raw_payload JSONB NOT NULL,
    
    processing_status VARCHAR(30) DEFAULT 'pending' 
        CHECK (processing_status IN ('pending', 'processing', 'processed', 'failed', 'skipped')),
    retry_count INT DEFAULT 0,
    last_error TEXT,
    
    created_at TIMESTAMPTZ DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_raw_events_pending 
ON raw_events (created_at ASC) 
WHERE processing_status = 'pending';

CREATE INDEX IF NOT EXISTS idx_raw_events_lookup 
ON raw_events (tenant_id, source_type, external_id);

-- 5. UNIFIED TASKS, SOURCES, COMMITMENTS, EVIDENCE
CREATE TABLE IF NOT EXISTS unified_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    status VARCHAR(30) DEFAULT 'open' 
        CHECK (status IN ('open', 'in_progress', 'likely_done', 'done', 'blocked', 'dismissed')),
    
    owner_id UUID NOT NULL REFERENCES people(id),
    requester_id UUID REFERENCES people(id),
    project_key VARCHAR(100),
    customer_id VARCHAR(100),
    
    due_date TIMESTAMPTZ,
    explicit_deadline BOOLEAN DEFAULT FALSE,
    
    extraction_confidence FLOAT NOT NULL DEFAULT 1.0,
    review_status VARCHAR(30) DEFAULT 'auto_approved'
        CHECK (review_status IN ('auto_approved', 'pending_review', 'rejected', 'user_created')),
    
    created_at TIMESTAMPTZ DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_unified_tasks_workspace_status 
ON unified_tasks (workspace_id, status);

CREATE INDEX IF NOT EXISTS idx_unified_tasks_owner 
ON unified_tasks (owner_id, status);

CREATE TABLE IF NOT EXISTS task_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES unified_tasks(id) ON DELETE CASCADE,
    source_type VARCHAR(50) NOT NULL,
    tenant_id VARCHAR(100) NOT NULL,
    external_id VARCHAR(255) NOT NULL,
    url TEXT,
    is_primary BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT clock_timestamp(),
    UNIQUE(source_type, tenant_id, external_id)
);

CREATE TABLE IF NOT EXISTS commitments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES unified_tasks(id) ON DELETE CASCADE,
    promiser_id UUID NOT NULL REFERENCES people(id),
    promisee_id UUID NOT NULL REFERENCES people(id),
    commitment_text TEXT NOT NULL,
    promised_at TIMESTAMPTZ NOT NULL,
    deadline TIMESTAMPTZ,
    is_fulfilled BOOLEAN DEFAULT FALSE,
    fulfilled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_commitments_promiser 
ON commitments (promiser_id, is_fulfilled, promised_at);

CREATE TABLE IF NOT EXISTS evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES unified_tasks(id) ON DELETE CASCADE,
    raw_event_id UUID REFERENCES raw_events(id),
    evidence_type VARCHAR(50) NOT NULL,
    source_type VARCHAR(50) NOT NULL,
    author_id UUID REFERENCES people(id),
    snippet TEXT NOT NULL,
    external_url TEXT,
    confidence FLOAT NOT NULL DEFAULT 1.0,
    extraction_version VARCHAR(50) DEFAULT 'v1.0',
    created_at TIMESTAMPTZ DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_evidence_task ON evidence (task_id);

-- 6. STATUS HISTORY, PRIORITY SCORES, REVIEW QUEUE, SETTINGS
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
    item_type VARCHAR(50) NOT NULL,
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

-- 7. TRANSACTIONAL GRAPH OUTBOX
CREATE TABLE IF NOT EXISTS graph_outbox_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    aggregate_type VARCHAR(50) NOT NULL, -- 'Task', 'Person', 'Project', 'Decision'
    aggregate_id VARCHAR(100) NOT NULL,
    action VARCHAR(50) NOT NULL, -- 'upsert_node', 'upsert_edge', 'invalidate_edge'
    
    node_label VARCHAR(100),
    edge_type VARCHAR(100),
    source_canonical_id VARCHAR(100),
    target_canonical_id VARCHAR(100),
    
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(30) DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    retry_count INT DEFAULT 0,
    last_error TEXT,
    
    created_at TIMESTAMPTZ DEFAULT clock_timestamp(),
    processed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending 
ON graph_outbox_events (created_at ASC) 
WHERE status = 'pending';

-- 8. TRIGGERS & RLS
CREATE OR REPLACE FUNCTION trigger_set_timestamp()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = clock_timestamp();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS set_timestamp_unified_tasks ON unified_tasks;
CREATE TRIGGER set_timestamp_unified_tasks
BEFORE UPDATE ON unified_tasks
FOR EACH ROW
EXECUTE FUNCTION trigger_set_timestamp();

DROP TRIGGER IF EXISTS set_timestamp_source_connections ON source_connections;
CREATE TRIGGER set_timestamp_source_connections
BEFORE UPDATE ON source_connections
FOR EACH ROW
EXECUTE FUNCTION trigger_set_timestamp();

DROP TRIGGER IF EXISTS set_timestamp_sync_checkpoints ON sync_checkpoints;
CREATE TRIGGER set_timestamp_sync_checkpoints
BEFORE UPDATE ON sync_checkpoints
FOR EACH ROW
EXECUTE FUNCTION trigger_set_timestamp();

CREATE OR REPLACE FUNCTION trigger_log_task_status_change()
RETURNS TRIGGER AS $$
BEGIN
  IF (OLD.status IS DISTINCT FROM NEW.status) THEN
    INSERT INTO task_status_history (task_id, previous_status, new_status, reason)
    VALUES (NEW.id, OLD.status, NEW.status, 'Status updated in unified_tasks');
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS log_task_status_change ON unified_tasks;
CREATE TRIGGER log_task_status_change
AFTER UPDATE ON unified_tasks
FOR EACH ROW
EXECUTE FUNCTION trigger_log_task_status_change();

-- Kích hoạt RLS
ALTER TABLE people ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE unified_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE commitments ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE review_queue ENABLE ROW LEVEL SECURITY;

-- Cấp quyền cho service_role và authenticated
CREATE POLICY "Allow service_role full access to people" ON people FOR ALL TO service_role USING (true);
CREATE POLICY "Allow service_role full access to source_identities" ON source_identities FOR ALL TO service_role USING (true);
CREATE POLICY "Allow service_role full access to source_connections" ON source_connections FOR ALL TO service_role USING (true);
CREATE POLICY "Allow service_role full access to unified_tasks" ON unified_tasks FOR ALL TO service_role USING (true);
CREATE POLICY "Allow service_role full access to commitments" ON commitments FOR ALL TO service_role USING (true);
CREATE POLICY "Allow service_role full access to evidence" ON evidence FOR ALL TO service_role USING (true);
CREATE POLICY "Allow service_role full access to review_queue" ON review_queue FOR ALL TO service_role USING (true);
