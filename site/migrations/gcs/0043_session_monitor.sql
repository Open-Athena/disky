-- Persistent notification budget and overlap protection, independent of isolates.
-- Alert attempts consume the budget BEFORE Slack I/O; ambiguous failures are not retried.
CREATE TABLE session_monitor (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  version INTEGER NOT NULL DEFAULT 0,
  lease_until INTEGER NOT NULL DEFAULT 0,
  checked_s INTEGER NOT NULL DEFAULT 0,
  signal_s INTEGER NOT NULL DEFAULT 0,
  alert_s INTEGER NOT NULL DEFAULT 0,
  day TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  thread_ts TEXT,
  delivery TEXT NOT NULL DEFAULT 'none'
);
INSERT INTO session_monitor (id) VALUES (1);
