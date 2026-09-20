/**
 * Shared TypeScript definitions matching ptb_contracts Pydantic models.
 * Generated for @ptb/contracts.
 */

// ==========================================
// Layer 1: Data Acquisition
// ==========================================

export type SourceType = "ms_teams" | "ms_outlook" | "jira" | "shortcut" | "confluence";

export type ProcessingStatus = "pending" | "processing" | "processed" | "failed" | "skipped";

export interface RawEventRecord {
  id: string;
  tenant_id: string;
  source_type: SourceType;
  external_id: string;
  parent_external_id?: string | null;
  idempotency_key: string;
  event_timestamp: string; // ISO string
  author_external_id: string;
  author_display_name?: string | null;
  conversation_or_project_id: string;
  deep_link?: string | null;
  raw_payload: Record<string, unknown>;
  processing_status: ProcessingStatus;
  retry_count: number;
  last_error?: string | null;
  created_at?: string | null;
}

export interface SourceConnectionConfig {
  id: string;
  user_id: string;
  tenant_id: string;
  source_type: SourceType;
  auth_type: string;
  encrypted_credentials: string;
  token_expires_at?: string | null;
  is_active: boolean;
}

export interface SyncCheckpointState {
  id: string;
  connection_id: string;
  sync_mode: string;
  page_token?: string | null;
  delta_token?: string | null;
  last_event_timestamp?: string | null;
  last_successful_sync_at?: string | null;
  status: string;
  error_message?: string | null;
}

// ==========================================
// Layer 2: Data Processing
// ==========================================

export type TaskStatus = "open" | "in_progress" | "likely_done" | "done" | "blocked" | "dismissed";

export type EvidenceType =
  | "chat_commitment"
  | "chat_request"
  | "jira_ticket"
  | "shortcut_story"
  | "email_thread"
  | "completion_signal";

export interface ParsedMessageContent {
  is_quote_reply: boolean;
  quoted_author_raw?: string | null;
  quoted_content_text?: string | null;
  actual_content_text: string;
}

export interface EvidenceRecord {
  id: string;
  task_id?: string | null;
  raw_event_id: string;
  evidence_type: EvidenceType;
  source_type: string;
  external_url?: string | null;
  author_canonical_id: string;
  timestamp: string;
  snippet: string;
  confidence: number;
  extraction_version: string;
}

export interface ExtractedCommitment {
  title: string;
  owner_id: string;
  requester_id?: string | null;
  project_key?: string | null;
  due_date?: string | null;
  explicit_deadline: boolean;
  confidence: number;
  evidence_snippet: string;
  raw_event_id: string;
}

export interface UnifiedTaskCandidate {
  id: string;
  title: string;
  description?: string | null;
  status: TaskStatus;
  owner_canonical_id: string;
  requester_canonical_id?: string | null;
  project_key?: string | null;
  customer_id?: string | null;
  due_date?: string | null;
  explicit_deadline: boolean;
  extraction_confidence: number;
  review_status: string;
  evidences: EvidenceRecord[];
}

export interface ReviewQueueItem {
  id: string;
  raw_event_id: string;
  candidate_task: UnifiedTaskCandidate;
  reason: string;
  created_at: string;
}

// ==========================================
// Layer 3: Storage & Knowledge Graph
// ==========================================

export type GraphActionType = "upsert_node" | "upsert_edge" | "invalidate_edge";

export interface CanonicalPersonRecord {
  id: string;
  workspace_id: string;
  canonical_name: string;
  primary_email: string;
  avatar_url?: string | null;
  is_current_user: boolean;
  created_at?: string | null;
}

export interface SourceIdentityRecord {
  id: string;
  person_id: string;
  tenant_id: string;
  source_type: string;
  external_id: string;
  external_username?: string | null;
  external_display_name?: string | null;
}

export interface GraphOutboxEventPayload {
  outbox_id?: string | null;
  aggregate_type: string;
  aggregate_id: string;
  action: GraphActionType;
  node_label?: string | null;
  node_properties?: Record<string, unknown> | null;
  edge_type?: string | null;
  source_canonical_id?: string | null;
  target_canonical_id?: string | null;
  edge_properties?: Record<string, unknown> | null;
  valid_at: string;
  confidence: number;
  evidence_id?: string | null;
}

// ==========================================
// Layer 4: Intelligence
// ==========================================

export interface GraphRelationInfo {
  relation_type: string;
  target_entity_type: string;
  target_entity_id: string;
  target_title_or_name: string;
  valid_since: string;
  confidence: number;
}

export interface TaskWithContext {
  task: UnifiedTaskCandidate;
  last_status_change_at: string;
  days_in_current_status: number;
  has_completion_evidence: boolean;
  relations: GraphRelationInfo[];
  blocking_tasks: string[];
  dependent_people: string[];
  related_decisions: string[];
  past_lessons_learned: string[];
}

export interface PriorityBreakdown {
  total_score: number;
  deadline_score: number;
  customer_impact_score: number;
  production_impact_score: number;
  commitment_weight: number;
  waiting_penalty: number;
  stale_age_score: number;
  uncertainty_deduction: number;
  llm_explanation: string;
}

export interface TodayTaskItem {
  task_id: string;
  title: string;
  status: TaskStatus;
  project_key?: string | null;
  owner_name: string;
  requester_name?: string | null;
  due_date?: string | null;
  priority: PriorityBreakdown;
  primary_evidence_snippet: string;
  deep_link?: string | null;
  is_at_risk: boolean;
  risk_reason?: string | null;
}

export interface ForgottenCommitmentItem {
  commitment_id: string;
  task_id: string;
  title: string;
  promised_to_name: string;
  promised_at: string;
  days_stale: number;
  last_conversation_snippet: string;
  suggested_action: string;
}

export interface WaitingOnItem {
  task_id: string;
  title: string;
  waiting_for_person_name: string;
  blocked_since: string;
  waiting_days: number;
  reason: string;
}

export interface TodayBoardView {
  generated_at: string;
  user_id: string;
  summary_headline: string;
  top_tasks: TodayTaskItem[];
  waiting_on_others: WaitingOnItem[];
  forgotten_commitments: ForgottenCommitmentItem[];
  identified_risks: string[];
}

export interface DecisionRecord {
  decision_id: string;
  project_key?: string | null;
  summary: string;
  rationale: string;
  decided_by: string;
  decided_at: string;
}

export interface LessonRecord {
  lesson_id: string;
  topic: string;
  description: string;
  solution: string;
  related_incident_id?: string | null;
  recorded_at: string;
}

// ==========================================
// Layer 5: Experience
// ==========================================

export interface TaskActionResponse {
  success: boolean;
  task_id: string;
  message: string;
  updated_at: string;
}

export interface ReviewActionResponse {
  success: boolean;
  review_id: string;
  action_taken: string;
  message: string;
}

export interface CoverageTenantStatus {
  tenant_id: string;
  tenant_name: string;
  source_type: string;
  status: string;
  last_successful_sync?: string | null;
  items_synced_total: number;
  error_message?: string | null;
}

export interface CoverageStatusResponse {
  tenants: CoverageTenantStatus[];
  overall_health: string;
  last_checked_at: string;
}

export interface KnowledgeSearchRequest {
  query: string;
  project_key?: string | null;
  knowledge_type: string;
  limit: number;
}

export interface KnowledgeSearchResponse {
  decisions: DecisionRecord[];
  lessons: LessonRecord[];
  synthesis_summary?: string | null;
}
