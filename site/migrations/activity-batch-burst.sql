-- A session-log batch's activity burst (`feedClient` links it): a burst's client error lines are read from its batches
-- (`activity.batchErrors`), not copied into `activity_events`.
ALTER TABLE session_log_batches ADD COLUMN burst_id INTEGER;
CREATE INDEX idx_session_log_batches_burst ON session_log_batches (burst_id);
-- Batches fed before this: the newest burst that has their session and spans their last event.
UPDATE session_log_batches SET burst_id = (
  SELECT b.id FROM activity_bursts b
  WHERE EXISTS (SELECT 1 FROM json_each(b.sids) WHERE value = session_log_batches.sid)
    AND session_log_batches.last_ms / 1000 BETWEEN b.first_ts AND b.last_ts
  ORDER BY b.id DESC LIMIT 1
);
-- The copies they replace.
DELETE FROM activity_events WHERE kind = 'error';
