-- ==============================================================================
-- PERSONAL TASK BOARD: DEVELOPMENT SEED DATA
-- Chạy sau khi đã chạy consolidated_schema.sql
-- ==============================================================================

-- 1. Workspace Settings
INSERT INTO workspace_settings (workspace_id, settings)
VALUES (
    '00000000-0000-0000-0000-000000000001',
    '{"workspace_name": "Personal Workspace", "default_timezone": "Asia/Ho_Chi_Minh", "priority_weights": {"deadline": 30, "customer": 20, "production": 20, "commitment": 15, "waiting": 15, "stale": 10}}'::jsonb
) ON CONFLICT (workspace_id) DO NOTHING;

-- 2. People
-- Current User: Dam Quang Cuong
INSERT INTO people (id, workspace_id, canonical_name, primary_email, is_current_user)
VALUES (
    '00000000-0000-0000-0000-000000000002',
    '00000000-0000-0000-0000-000000000001',
    'Dam Quang Cuong',
    'cuong.dam@fpt.com',
    TRUE
) ON CONFLICT (primary_email) DO NOTHING;

-- Teammate: Nguyen Van Huy
INSERT INTO people (id, workspace_id, canonical_name, primary_email, is_current_user)
VALUES (
    '00000000-0000-0000-0000-000000000003',
    '00000000-0000-0000-0000-000000000001',
    'Nguyen Van Huy',
    'huy.nguyen@fpt.com',
    FALSE
) ON CONFLICT (primary_email) DO NOTHING;

-- Teammate: Tran Thi Mai (PM/Lead)
INSERT INTO people (id, workspace_id, canonical_name, primary_email, is_current_user)
VALUES (
    '00000000-0000-0000-0000-000000000004',
    '00000000-0000-0000-0000-000000000001',
    'Tran Thi Mai',
    'mai.tran@fpt.com',
    FALSE
) ON CONFLICT (primary_email) DO NOTHING;

-- 3. Source Identities Mapping
-- Cuong's identities across platforms
INSERT INTO source_identities (person_id, tenant_id, source_type, external_id, external_display_name)
VALUES 
(
    '00000000-0000-0000-0000-000000000002',
    'tenant-fpt-internal',
    'ms_teams',
    'cuong.dam@fpt.com',
    'Dam Quang Cuong'
),
(
    '00000000-0000-0000-0000-000000000002',
    'tenant-fpt-internal',
    'jira',
    'cuong.dam_jira',
    'Dam Quang Cuong'
),
(
    '00000000-0000-0000-0000-000000000002',
    'tenant-fpt-internal',
    'shortcut',
    'cuong-shortcut-id',
    'cuongdam'
) ON CONFLICT (tenant_id, source_type, external_id) DO NOTHING;

-- Huy's identities
INSERT INTO source_identities (person_id, tenant_id, source_type, external_id, external_display_name)
VALUES 
(
    '00000000-0000-0000-0000-000000000003',
    'tenant-fpt-internal',
    'ms_teams',
    'huy.nguyen@fpt.com',
    'Nguyen Van Huy'
),
(
    '00000000-0000-0000-0000-000000000003',
    'tenant-fpt-internal',
    'jira',
    'huy.nguyen_jira',
    'Nguyen Van Huy'
) ON CONFLICT (tenant_id, source_type, external_id) DO NOTHING;
