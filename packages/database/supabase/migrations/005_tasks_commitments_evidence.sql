-- Migration 005: Tasks, Commitments, Evidence, Task Sources

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
