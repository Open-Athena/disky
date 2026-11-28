-- Slack in Workers (specs/slack-in-workers.md): one row per Slack job the site runs in steps (`_lib/slackJobs.ts`). A
-- job is a converge: its own progress lives in what it writes (the digest's thread state), so a row holds only what a
-- step needs to resume one: its lease, what not to repeat within the job (`memo`), its Slack call count, a shadow run's
-- recorded writes and audit (`report`). One live job per `key` (`<kind>:<channel>/<month>`, a shadow's prefixed
-- `shadow:`): a second request for a thread joins the live one.
CREATE TABLE slack_jobs (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,
  key         TEXT NOT NULL,
  args        TEXT NOT NULL,
  state       TEXT NOT NULL CHECK (state IN ('pending', 'running', 'done', 'failed')),
  shadow      INTEGER NOT NULL DEFAULT 0,
  memo        TEXT NOT NULL DEFAULT '{}',
  report      TEXT,
  lease_until INTEGER NOT NULL DEFAULT 0,
  retry_at    INTEGER,
  calls       INTEGER NOT NULL DEFAULT 0,
  steps       INTEGER NOT NULL DEFAULT 0,
  error       TEXT,
  created_by  TEXT NOT NULL,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);
CREATE UNIQUE INDEX slack_jobs_live ON slack_jobs (key) WHERE state IN ('pending', 'running');
CREATE INDEX slack_jobs_updated ON slack_jobs (updated_at);
