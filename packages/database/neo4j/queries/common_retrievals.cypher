// ==============================================================================
// PERSONAL TASK BOARD: COMMON CYPHER QUERIES (LAYER 4 & GRAPHITI)
// Các mẫu truy vấn thường dùng trong hệ thống
// ==============================================================================

// 1. Lấy toàn bộ mạng lưới ngữ cảnh của một Task (Blockers, Owner, Requester, Project)
MATCH (t:Task {task_id: $task_id})
OPTIONAL MATCH (owner:Person)-[:OWNS]->(t)
OPTIONAL MATCH (req:Person)-[:REQUESTED]->(t)
OPTIONAL MATCH (t)-[:BELONGS_TO]->(p:Project)
OPTIONAL MATCH (t)-[:BLOCKED_BY]->(blocker:Task)
OPTIONAL MATCH (dependent:Person)-[:WAITING_FOR]->(owner)
RETURN t, owner, req, p, collect(blocker) AS blockers, collect(dependent) AS dependents;

// 2. Tìm kiếm các quyết định kỹ thuật ảnh hưởng đến dự án
MATCH (d:Decision)-[:AFFECTS]->(p:Project {project_key: $project_key})
WHERE d.invalid_at IS NULL
RETURN d.summary, d.rationale, d.decided_at, d.decided_by
ORDER BY d.decided_at DESC;

// 3. Tìm bài học kinh nghiệm từ các sự cố tương tự
MATCH (l:Lesson)-[:DERIVED_FROM]->(inc:Incident)
WHERE l.topic CONTAINS $keyword OR inc.severity = 'CRITICAL'
RETURN l.topic, l.solution, inc.severity, l.recorded_at
ORDER BY l.recorded_at DESC
LIMIT 5;

// 4. Lấy danh sách người đang bị chặn bởi công việc của user hiện tại
MATCH (me:Person {is_current_user: true})-[r:COMMITTED_TO]->(t:Task)
MATCH (other:Person)-[:WAITING_FOR]->(me)
WHERE t.status IN ['open', 'in_progress']
RETURN t.title, other.name, r.promised_at;
