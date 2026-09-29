// ==============================================================================
// PERSONAL TASK BOARD: COMMON CYPHER QUERIES (CANONICAL SINGLE-STORE)
// Các mẫu truy vấn thường dùng trong hệ thống theo docs/v1.md
// ==============================================================================

// 1. Lấy toàn bộ mạng lưới ngữ cảnh của một UnifiedTask (Blockers, Owner, Requester, Project, Evidence)
MATCH (t:UnifiedTask {id: $task_id})
OPTIONAL MATCH (owner:Person)-[:ASSIGNED_TO]->(t)
OPTIONAL MATCH (req:Person)-[:REQUESTED]->(t)
OPTIONAL MATCH (t)-[:BELONGS_TO]->(p:Project)
OPTIONAL MATCH (t)-[:BLOCKED_BY]->(blocker:UnifiedTask)
OPTIONAL MATCH (t)-[:HAS_EVIDENCE]->(e:Evidence)
OPTIONAL MATCH (dependent:Person)-[:WAITING_FOR]->(owner)
RETURN t, owner, req, p, collect(blocker) AS blockers, collect(e) AS evidences, collect(dependent) AS dependents;

// 2. Tìm kiếm các quyết định kỹ thuật ảnh hưởng đến dự án hoặc task
MATCH (d:Decision)-[:AFFECTS]->(target)
WHERE (target:Project AND target.project_key = $project_key) OR (target:UnifiedTask AND target.id = $task_id)
RETURN d.summary, d.rationale, d.decided_at, d.decided_by
ORDER BY d.decided_at DESC;

// 3. Tìm bài học kinh nghiệm từ các sự cố hoặc task tương tự
MATCH (l:Lesson)-[:DERIVED_FROM]->(source)
WHERE l.topic CONTAINS $keyword OR (source:Incident AND source.severity = 'CRITICAL')
RETURN l.topic, l.solution, l.recorded_at
ORDER BY l.recorded_at DESC
LIMIT 5;

// 4. Lấy danh sách việc user hiện tại đã cam kết
MATCH (me:Person {is_current_user: true})-[:COMMITTED_TO]->(t:UnifiedTask)
OPTIONAL MATCH (other:Person)-[:WAITING_FOR]->(me)
WHERE t.status IN ['TODO', 'IN_PROGRESS']
RETURN t.id, t.title, t.priority_score, t.due_date, collect(other.canonical_name) AS waiting_people;
