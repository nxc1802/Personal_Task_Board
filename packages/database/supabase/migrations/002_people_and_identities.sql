-- Migration 002: People and Identities

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
