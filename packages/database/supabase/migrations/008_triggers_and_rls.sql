-- Migration 008: Triggers and Row Level Security (RLS)

-- 1. Trigger Function: Cập nhật updated_at tự động
CREATE OR REPLACE FUNCTION trigger_set_timestamp()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = clock_timestamp();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS set_timestamp_unified_tasks ON unified_tasks;
CREATE TRIGGER set_timestamp_unified_tasks
BEFORE UPDATE ON unified_tasks
FOR EACH ROW
EXECUTE FUNCTION trigger_set_timestamp();

DROP TRIGGER IF EXISTS set_timestamp_source_connections ON source_connections;
CREATE TRIGGER set_timestamp_source_connections
BEFORE UPDATE ON source_connections
FOR EACH ROW
EXECUTE FUNCTION trigger_set_timestamp();

DROP TRIGGER IF EXISTS set_timestamp_sync_checkpoints ON sync_checkpoints;
CREATE TRIGGER set_timestamp_sync_checkpoints
BEFORE UPDATE ON sync_checkpoints
FOR EACH ROW
EXECUTE FUNCTION trigger_set_timestamp();

-- 2. Trigger Function: Tự động ghi task_status_history khi status thay đổi
CREATE OR REPLACE FUNCTION trigger_log_task_status_change()
RETURNS TRIGGER AS $$
BEGIN
  IF (OLD.status IS DISTINCT FROM NEW.status) THEN
    INSERT INTO task_status_history (task_id, previous_status, new_status, reason)
    VALUES (NEW.id, OLD.status, NEW.status, 'Status updated in unified_tasks');
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS log_task_status_change ON unified_tasks;
CREATE TRIGGER log_task_status_change
AFTER UPDATE ON unified_tasks
FOR EACH ROW
EXECUTE FUNCTION trigger_log_task_status_change();

-- 3. Kích hoạt Row Level Security (RLS) trên các bảng hoạt động chính
ALTER TABLE people ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE unified_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE commitments ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE review_queue ENABLE ROW LEVEL SECURITY;

-- Tạo RLS policies cho phép truy cập qua authenticated user hoặc service role
CREATE POLICY "Allow all access to service_role" ON people FOR ALL TO service_role USING (true);
CREATE POLICY "Allow all access to service_role" ON source_identities FOR ALL TO service_role USING (true);
CREATE POLICY "Allow all access to service_role" ON source_connections FOR ALL TO service_role USING (true);
CREATE POLICY "Allow all access to service_role" ON unified_tasks FOR ALL TO service_role USING (true);
CREATE POLICY "Allow all access to service_role" ON commitments FOR ALL TO service_role USING (true);
CREATE POLICY "Allow all access to service_role" ON evidence FOR ALL TO service_role USING (true);
CREATE POLICY "Allow all access to service_role" ON review_queue FOR ALL TO service_role USING (true);
