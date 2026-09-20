-- Migration 003: Source Connections and Sync Checkpoints

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
