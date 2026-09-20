-- Migration 004: Raw Events (Immutable Layer 1 Ingestion Store)

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

-- Index cho Layer 2 worker nhặt pending events theo thứ tự thời gian
CREATE INDEX IF NOT EXISTS idx_raw_events_pending 
ON raw_events (created_at ASC) 
WHERE processing_status = 'pending';

-- Index phục vụ tra cứu nhanh nguồn gốc
CREATE INDEX IF NOT EXISTS idx_raw_events_lookup 
ON raw_events (tenant_id, source_type, external_id);
