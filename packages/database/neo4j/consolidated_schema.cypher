// ==============================================================================
// PERSONAL TASK BOARD: CONSOLIDATED NEO4J AURADB SCHEMA
// Chạy file này trong Neo4j Aura Console (Open Query) hoặc cypher-shell
// ==============================================================================

// 1. UNIQUENESS CONSTRAINTS CHO 11 NODE TYPES
CREATE CONSTRAINT c_person_id IF NOT EXISTS 
FOR (p:Person) REQUIRE p.canonical_id IS UNIQUE;

CREATE CONSTRAINT c_source_identity_key IF NOT EXISTS 
FOR (s:SourceIdentity) REQUIRE s.identity_key IS UNIQUE;

CREATE CONSTRAINT c_task_id IF NOT EXISTS 
FOR (t:Task) REQUIRE t.task_id IS UNIQUE;

CREATE CONSTRAINT c_project_key IF NOT EXISTS 
FOR (pr:Project) REQUIRE pr.project_key IS UNIQUE;

CREATE CONSTRAINT c_customer_id IF NOT EXISTS 
FOR (c:Customer) REQUIRE c.customer_id IS UNIQUE;

CREATE CONSTRAINT c_tenant_id IF NOT EXISTS 
FOR (tn:Tenant) REQUIRE tn.tenant_id IS UNIQUE;

CREATE CONSTRAINT c_source_item_id IF NOT EXISTS 
FOR (si:SourceItem) REQUIRE si.item_id IS UNIQUE;

CREATE CONSTRAINT c_decision_id IF NOT EXISTS 
FOR (d:Decision) REQUIRE d.decision_id IS UNIQUE;

CREATE CONSTRAINT c_lesson_id IF NOT EXISTS 
FOR (l:Lesson) REQUIRE l.lesson_id IS UNIQUE;

CREATE CONSTRAINT c_document_id IF NOT EXISTS 
FOR (doc:Document) REQUIRE doc.doc_id IS UNIQUE;

CREATE CONSTRAINT c_incident_id IF NOT EXISTS 
FOR (inc:Incident) REQUIRE inc.incident_id IS UNIQUE;

// 2. INDEXES TỐI ƯU HÓA TRUY VẤN
CREATE INDEX idx_task_status IF NOT EXISTS 
FOR (t:Task) ON (t.status);

CREATE INDEX idx_person_email IF NOT EXISTS 
FOR (p:Person) ON (p.email);

CREATE INDEX idx_decision_topic IF NOT EXISTS 
FOR (d:Decision) ON (d.topic);

CREATE INDEX idx_lesson_topic IF NOT EXISTS 
FOR (l:Lesson) ON (l.topic);
