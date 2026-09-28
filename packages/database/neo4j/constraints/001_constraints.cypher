// ==============================================================================
// PERSONAL TASK BOARD: NEO4J CANONICAL ONTOLOGY CONSTRAINTS & INDEXES
// Single-Store Architecture (docs/v1.md)
// ==============================================================================

// 1. UNIQUENESS CONSTRAINTS CHO 15 NODE TYPES
CREATE CONSTRAINT c_unified_task_id IF NOT EXISTS 
FOR (t:UnifiedTask) REQUIRE t.id IS UNIQUE;

CREATE CONSTRAINT c_evidence_id IF NOT EXISTS 
FOR (e:Evidence) REQUIRE e.id IS UNIQUE;

CREATE CONSTRAINT c_commitment_id IF NOT EXISTS 
FOR (c:Commitment) REQUIRE c.id IS UNIQUE;

CREATE CONSTRAINT c_person_id IF NOT EXISTS 
FOR (p:Person) REQUIRE p.canonical_id IS UNIQUE;

CREATE CONSTRAINT c_source_identity_key IF NOT EXISTS 
FOR (s:SourceIdentity) REQUIRE s.identity_key IS UNIQUE;

CREATE CONSTRAINT c_project_key IF NOT EXISTS 
FOR (pr:Project) REQUIRE pr.project_key IS UNIQUE;

CREATE CONSTRAINT c_customer_id IF NOT EXISTS 
FOR (c:Customer) REQUIRE c.customer_id IS UNIQUE;

CREATE CONSTRAINT c_tenant_id IF NOT EXISTS 
FOR (tn:Tenant) REQUIRE tn.tenant_id IS UNIQUE;

CREATE CONSTRAINT c_raw_event_id IF NOT EXISTS 
FOR (re:RawEvent) REQUIRE re.id IS UNIQUE;

CREATE CONSTRAINT c_raw_event_idempotency IF NOT EXISTS 
FOR (re:RawEvent) REQUIRE re.idempotency_key IS UNIQUE;

CREATE CONSTRAINT c_checkpoint_id IF NOT EXISTS 
FOR (cp:IngestionCheckpoint) REQUIRE cp.id IS UNIQUE;

CREATE CONSTRAINT c_checkpoint_composite IF NOT EXISTS 
FOR (cp:IngestionCheckpoint) REQUIRE (cp.tenant_id, cp.source_type, cp.stream_id) IS UNIQUE;

CREATE CONSTRAINT c_processing_attempt_id IF NOT EXISTS 
FOR (pa:ProcessingAttempt) REQUIRE pa.id IS UNIQUE;

CREATE CONSTRAINT c_decision_id IF NOT EXISTS 
FOR (d:Decision) REQUIRE d.decision_id IS UNIQUE;

CREATE CONSTRAINT c_lesson_id IF NOT EXISTS 
FOR (l:Lesson) REQUIRE l.lesson_id IS UNIQUE;

CREATE CONSTRAINT c_document_id IF NOT EXISTS 
FOR (doc:Document) REQUIRE doc.doc_id IS UNIQUE;

CREATE CONSTRAINT c_incident_id IF NOT EXISTS 
FOR (inc:Incident) REQUIRE inc.incident_id IS UNIQUE;

CREATE CONSTRAINT c_status_transition_audit_id IF NOT EXISTS 
FOR (sta:StatusTransitionAudit) REQUIRE sta.id IS UNIQUE;

CREATE CONSTRAINT c_merge_audit_id IF NOT EXISTS 
FOR (ma:MergeAudit) REQUIRE ma.id IS UNIQUE;

// 2. INDEXES TỐI ƯU HÓA TRUY VẤN
CREATE INDEX idx_task_status IF NOT EXISTS 
FOR (t:UnifiedTask) ON (t.status);

CREATE INDEX idx_task_due_date IF NOT EXISTS 
FOR (t:UnifiedTask) ON (t.due_date);

CREATE INDEX idx_task_priority IF NOT EXISTS 
FOR (t:UnifiedTask) ON (t.priority_score);

CREATE INDEX idx_raw_event_status IF NOT EXISTS 
FOR (re:RawEvent) ON (re.processing_status);

CREATE INDEX idx_raw_event_timestamp IF NOT EXISTS 
FOR (re:RawEvent) ON (re.event_timestamp);

CREATE INDEX idx_person_email IF NOT EXISTS 
FOR (p:Person) ON (p.primary_email);

CREATE INDEX idx_decision_topic IF NOT EXISTS 
FOR (d:Decision) ON (d.topic);

CREATE INDEX idx_lesson_topic IF NOT EXISTS 
FOR (l:Lesson) ON (l.topic);
