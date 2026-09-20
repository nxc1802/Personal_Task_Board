-- Migration 007: Transactional Graph Outbox Events

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
