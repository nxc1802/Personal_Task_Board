// ==============================================================================
// PERSONAL TASK BOARD: NEO4J AURADB DEV SEED
// Nạp dữ liệu đồ thị ban đầu đồng bộ với Supabase 001_dev_seed.sql
// ==============================================================================

// 1. Tạo Canonical Person Nodes
MERGE (cuong:Person {canonical_id: "00000000-0000-0000-0000-000000000002"})
SET cuong.name = "Dam Quang Cuong",
    cuong.email = "cuong.dam@fpt.com",
    cuong.is_current_user = true,
    cuong.updated_at = datetime();

MERGE (huy:Person {canonical_id: "00000000-0000-0000-0000-000000000003"})
SET huy.name = "Nguyen Van Huy",
    huy.email = "huy.nguyen@fpt.com",
    huy.is_current_user = false,
    huy.updated_at = datetime();

MERGE (mai:Person {canonical_id: "00000000-0000-0000-0000-000000000004"})
SET mai.name = "Tran Thi Mai",
    mai.email = "mai.tran@fpt.com",
    mai.is_current_user = false,
    mai.updated_at = datetime();

// 2. Tạo Project Nodes
MERGE (ops:Project {project_key: "OPS"})
SET ops.name = "Operations Core",
    ops.updated_at = datetime();

// 3. Tạo SourceIdentity Nodes & Quan hệ HAS_IDENTITY
MERGE (id_teams_cuong:SourceIdentity {identity_key: "tenant-fpt-internal:ms_teams:cuong.dam@fpt.com"})
SET id_teams_cuong.tenant_id = "tenant-fpt-internal",
    id_teams_cuong.source_type = "ms_teams",
    id_teams_cuong.external_id = "cuong.dam@fpt.com";

MERGE (cuong)-[:HAS_IDENTITY]->(id_teams_cuong);

MERGE (id_jira_cuong:SourceIdentity {identity_key: "tenant-fpt-internal:jira:cuong.dam_jira"})
SET id_jira_cuong.tenant_id = "tenant-fpt-internal",
    id_jira_cuong.source_type = "jira",
    id_jira_cuong.external_id = "cuong.dam_jira";

MERGE (cuong)-[:HAS_IDENTITY]->(id_jira_cuong);

MERGE (id_teams_huy:SourceIdentity {identity_key: "tenant-fpt-internal:ms_teams:huy.nguyen@fpt.com"})
SET id_teams_huy.tenant_id = "tenant-fpt-internal",
    id_teams_huy.source_type = "ms_teams",
    id_teams_huy.external_id = "huy.nguyen@fpt.com";

MERGE (huy)-[:HAS_IDENTITY]->(id_teams_huy);
