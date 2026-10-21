-- The activity feed (specs/done/activity-feed.md): per-kind config, bursts, server events, and each burst's Slack / Discord
-- posts. Additive; no foreign keys (retention deletes in any order).
CREATE TABLE activity_kinds (
  kind   TEXT PRIMARY KEY,
  log    INTEGER NOT NULL,
  notify TEXT NOT NULL CHECK (notify IN ('off', 'count', 'line')),
  who    TEXT NOT NULL,
  ts     INTEGER NOT NULL
);

CREATE TABLE activity_bursts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  actor       TEXT NOT NULL,
  via         TEXT NOT NULL,
  origin      TEXT NOT NULL,
  first_ts    INTEGER NOT NULL,
  last_ts     INTEGER NOT NULL,
  counts      TEXT NOT NULL,
  pages       TEXT NOT NULL,
  sids        TEXT NOT NULL,
  rendered_ms INTEGER,
  dirty       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_activity_bursts_actor ON activity_bursts (actor, last_ts);
CREATE INDEX idx_activity_bursts_last ON activity_bursts (last_ts);

CREATE TABLE activity_events (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  burst_id INTEGER NOT NULL,
  ts       INTEGER NOT NULL,
  kind     TEXT NOT NULL,
  actor    TEXT NOT NULL,
  via      TEXT NOT NULL,
  method   TEXT NOT NULL,
  route    TEXT NOT NULL,
  line     TEXT NOT NULL,
  detail   TEXT
);
CREATE INDEX idx_activity_events_burst ON activity_events (burst_id, ts);
CREATE INDEX idx_activity_events_ts ON activity_events (ts);

CREATE TABLE activity_posts (
  burst_id   INTEGER NOT NULL,
  sink       TEXT NOT NULL,
  part       TEXT NOT NULL,
  msg_id     TEXT NOT NULL,
  claimed_ms INTEGER NOT NULL,
  PRIMARY KEY (burst_id, sink, part)
);
