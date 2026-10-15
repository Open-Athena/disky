-- The D1 schema: this branch's current state (specs/done/d1-migrations.md). A new database is created from it
-- (`wrangler d1 execute <db> --remote --file site/schema.sql`); a change lands as a recipe in `site/migrations/`
-- plus this file, regenerated with `dt-cloud d1-schema dump site/schema.sql site/migrations/<slug>.sql`.
-- Canonical form (`dt-cloud d1-schema dump`): edit through a recipe, not by hand.

CREATE TABLE access_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL,
  event       TEXT NOT NULL,   -- redeem | deny | revoke | request | view | signin | signout
  grant_id    TEXT,
  session_sub TEXT,            -- `e:<email>` | `g:<id>`
  path        TEXT,
  status      INTEGER,
  ip_hash     TEXT,
  ua          TEXT,
  country     TEXT,
  referer     TEXT,
  reason      TEXT,            -- deny detail: bad-token | revoked | expired | exhausted | not-allowed
  bucket      INTEGER          -- floor(ts/3600) for `view` rows; NULL otherwise
, city TEXT, region TEXT, as_org TEXT);

CREATE TABLE access_log_daily (
  day      INTEGER NOT NULL,   -- floor(ts / 86400)
  event    TEXT NOT NULL,
  grant_id TEXT,
  path     TEXT,
  country  TEXT,
  events   INTEGER NOT NULL,   -- rows collapsed into this bucket
  clients  INTEGER NOT NULL,   -- distinct ip_hash values within it
  PRIMARY KEY (day, event, grant_id, path, country)
);

CREATE TABLE access_requests (
  id         TEXT PRIMARY KEY,
  email      TEXT NOT NULL,
  name       TEXT,
  note       TEXT,
  created_at INTEGER NOT NULL,
  status     TEXT NOT NULL,   -- pending | approved | denied | auto
  decided_at INTEGER,
  decided_by TEXT,
  grant_id   TEXT,
  ip_hash    TEXT             -- HMAC, for rate-limiting; never a raw address
, subject_json TEXT);

CREATE TABLE admin_edits (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  tbl      TEXT NOT NULL,
  pk       TEXT NOT NULL,
  action   TEXT NOT NULL CHECK (action IN ('insert', 'update', 'delete')),
  who      TEXT NOT NULL,
  ts       INTEGER NOT NULL,
  old_json TEXT,
  new_json TEXT
);

CREATE TABLE admin_emails (
  email TEXT PRIMARY KEY,               -- lowercased; may mark sweep + dispatch runs
  note  TEXT,                           -- who this is
  who   TEXT NOT NULL,                  -- admin who added the row
  ts    INTEGER NOT NULL                -- epoch seconds
);

CREATE TABLE agent_tokens (
  email    TEXT PRIMARY KEY,          -- lower-cased owner email
  grant_id TEXT NOT NULL,             -- grants.id of the live token grant
  created  INTEGER NOT NULL           -- unix seconds, server-assigned
);

CREATE TABLE allowed_emails (
  email TEXT PRIMARY KEY,               -- lowercased
  note  TEXT,                           -- who this is / where they're from
  who   TEXT NOT NULL,                  -- admin who added the row
  ts    INTEGER NOT NULL                -- epoch seconds
, read_only INTEGER NOT NULL DEFAULT 0);

CREATE TABLE footer_cache (
  date            TEXT NOT NULL,
  variant         TEXT NOT NULL,
  gen             TEXT NOT NULL,
  state           TEXT NOT NULL CHECK (state IN ('filling', 'hot', 'evicting')),
  rows            INTEGER NOT NULL DEFAULT 0,  -- index_row_groups rows the entry holds
  bytes           INTEGER NOT NULL DEFAULT 0,  -- their text columns' lengths + 64 per row
  started_ts      INTEGER NOT NULL,            -- epoch s: the fill's claim, or the demotion
  filled_ts       INTEGER,                     -- epoch s the last batch landed
  last_access_day INTEGER NOT NULL DEFAULT 0,  -- epoch day of the newest read (written ≤ once a day)
  PRIMARY KEY (date, variant, gen)
);

CREATE TABLE grants (
  id            TEXT PRIMARY KEY,       -- random, not autoincrement: doesn't leak counts
  token_hash    TEXT NOT NULL UNIQUE,   -- SHA-256, base64url
  name          TEXT,                   -- admin-side label: "Bob Smith (donor)"
  note          TEXT,                   -- freeform: why this exists
  subject_json  TEXT,                   -- optional pre-loaded identity: {first,last,email,avatar}
  email         TEXT,                   -- if set: magic-link semantics (bind on redeem)
  scopes        TEXT NOT NULL,          -- space-separated
  max_redeems   INTEGER,                -- NULL = unlimited; counts sessions minted, not requests
  redeems       INTEGER NOT NULL DEFAULT 0,
  expires_at    INTEGER,                -- NULL = never
  session_ttl   INTEGER,                -- seconds; NULL = inherit app default
  created_at    INTEGER NOT NULL,
  created_by    TEXT NOT NULL,
  revoked_at    INTEGER,
  first_used_at INTEGER,
  last_used_at  INTEGER
, disabled_at INTEGER, expiry_ends_sessions INTEGER NOT NULL DEFAULT 1, sessions_invalid_before INTEGER);

CREATE TABLE index_row_groups (
  date      TEXT NOT NULL,
  variant   TEXT NOT NULL,
  gen       TEXT NOT NULL,
  rg        INTEGER NOT NULL,   -- row-group ordinal
  d_min     INTEGER NOT NULL,   -- depth stats (row-group pruning)
  d_max     INTEGER NOT NULL,
  p_min     TEXT NOT NULL,      -- path stats
  p_max     TEXT NOT NULL,
  b_max     INTEGER NOT NULL,   -- max descendant-inclusive bytes (threshold prune)
  u_min     TEXT,               -- usr range (a user-sorted variant's primary key; NULL here)
  u_max     TEXT,
  row_start INTEGER NOT NULL,   -- absolute row range of this group
  row_end   INTEGER NOT NULL,
  rg_json   TEXT NOT NULL, store TEXT NOT NULL DEFAULT 'primary',      -- compact [num_rows, codec, [[off, size, dict_off], …]]
  PRIMARY KEY (date, variant, gen, rg)
);

CREATE TABLE "index_schema" (
  store       TEXT NOT NULL DEFAULT 'primary',  -- 'primary' or a `STORES_JSON` key
  date        TEXT NOT NULL,            -- scan id (SNAP_ID, e.g. 2026-09-16T0001)
  variant     TEXT NOT NULL DEFAULT 'path',  -- 'path' (floor-free) or 'coarse<E>'
  version     INTEGER NOT NULL,         -- parquet FileMetaData.version
  schema_json TEXT NOT NULL,            -- hyparquet SchemaElement[] (root + leaves)
  floor_bytes INTEGER,                  -- a coarse tier's absolute byte floor F; NULL = floor-free
  gen         TEXT,                     -- generation stamp of the synced file set
  dir         TEXT, started TEXT,                     -- bucket-relative dir holding this variant's parquet
  PRIMARY KEY (store, date, variant)
);

CREATE TABLE og_tokens (
  id         INTEGER PRIMARY KEY,
  token      TEXT NOT NULL,
  kind       TEXT NOT NULL,     -- the card kind (`map`, `staged`, …)
  view       TEXT NOT NULL,     -- the canonical view params it covers
  page       TEXT NOT NULL,     -- the page URL path + view, without `og=`
  minted_by  TEXT NOT NULL,     -- the minter's email (or `slack:` for server posts)
  minted_ts  INTEGER NOT NULL,
  exp_day    INTEGER NOT NULL,  -- days since 2026-01-01; valid through that day (UTC)
  revoked_by TEXT,
  revoked_ts INTEGER
);

CREATE TABLE owner_prefix_aggs (
  scan       TEXT NOT NULL,     -- scan id the prefixes are priced on
  idx        TEXT NOT NULL,     -- the index read (`<mode>:<gen>`): a reindex prices again
  body       TEXT NOT NULL,     -- JSON: { v, buckets, asked, aggs }
  updated_ts INTEGER NOT NULL,
  PRIMARY KEY (scan, idx)
);

CREATE TABLE owner_prefix_hist (
  idx        TEXT NOT NULL,     -- the interval store's index tag (`<mode>:<gen>`): a new generation prices again
  depth      INTEGER NOT NULL,  -- the prefix's depth; 0 = the roots marker (every depth-1 path priced through `hz`)
  path       TEXT NOT NULL,
  hz         INTEGER NOT NULL,  -- the newest scan ordinal (`vf`/`vt` units) the versions are complete through
  versions   TEXT NOT NULL,     -- JSON: [[vf, vt, agg], …], agg = { b, o, us, uo, cb }; absent between = not in the scan
  updated_ts INTEGER NOT NULL,
  PRIMARY KEY (idx, depth, path)
);

CREATE TABLE pending_auth (
  id          TEXT PRIMARY KEY,
  email       TEXT NOT NULL,
  token_hash  TEXT NOT NULL,
  code_hash   TEXT NOT NULL,
  created_at  INTEGER NOT NULL,
  expires_at  INTEGER NOT NULL,
  consumed_at INTEGER,
  attempts    INTEGER NOT NULL DEFAULT 0
, ip_hash TEXT);

CREATE TABLE plans (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  note       TEXT,
  state      TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed')),
  created_by TEXT NOT NULL,            -- CF Access identity that drafted it
  created_ts INTEGER NOT NULL,         -- epoch seconds
  closed_ts  INTEGER                   -- set when state → closed
, slack_channel TEXT, slack_ts TEXT);

CREATE TABLE deletion_runs (
  run_id TEXT PRIMARY KEY,           -- <plan>-<scan>/<utc compact ts>
  plan_id INTEGER NOT NULL REFERENCES plans(id),  -- the first-class plan executed
  manifest TEXT NOT NULL,            -- gs:// dir the object-level manifest was written to
  scan TEXT NOT NULL,
  head INTEGER NOT NULL,             -- mark_log head at pin (drift detection)
  exec_head INTEGER NOT NULL,        -- mark_log head at execution (re-verified)
  actor TEXT NOT NULL,               -- CF Access identity that dispatched
  mode TEXT NOT NULL CHECK (mode IN ('dry', 'real')),
  started_ts INTEGER NOT NULL,
  finished_ts INTEGER,
  deleted_bytes INTEGER NOT NULL DEFAULT 0,   -- would-delete bytes for dry runs
  deleted_objects INTEGER NOT NULL DEFAULT 0,
  skipped_gone INTEGER NOT NULL DEFAULT 0,     -- key already absent
  skipped_overwritten INTEGER NOT NULL DEFAULT 0, -- versionId/ETag drifted since scan
  drift_dirs INTEGER NOT NULL DEFAULT 0,      -- dirs skipped: new keys since the scan
  ledger_drift_dirs INTEGER NOT NULL DEFAULT 0, -- dirs dropped: newer marks
  buckets TEXT,                      -- bucket cut dispatched with (CSV); NULL = whole plan
  undo_deadline INTEGER,             -- finished_ts + hold window (real runs); undo before this
  undo_state TEXT NOT NULL DEFAULT 'none' CHECK (undo_state IN ('none', 'partial', 'full', 'expired')),
  purge_state TEXT NOT NULL DEFAULT 'none' CHECK (purge_state IN ('none', 'pending', 'done')),
  log_dir TEXT NOT NULL              -- gs:// dir holding {would-delete,deleted}/ parquets + summary
, plan_digest TEXT);

CREATE TABLE deletion_bands (
  run_id TEXT NOT NULL REFERENCES deletion_runs(run_id),
  prefix TEXT NOT NULL,              -- s3://<bucket>/<dir>/ band the deletions fell under
  bytes INTEGER NOT NULL,
  objects INTEGER NOT NULL,
  gone INTEGER NOT NULL DEFAULT 0,
  overwritten INTEGER NOT NULL DEFAULT 0,
  drift_new_objects INTEGER NOT NULL DEFAULT 0,
  undone_objects INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, prefix)
);

CREATE TABLE deletion_progress (
  run_id TEXT NOT NULL REFERENCES deletion_runs(run_id),
  bucket TEXT NOT NULL,
  ts REAL NOT NULL,
  deletes INTEGER NOT NULL,
  bytes INTEGER NOT NULL,
  gone INTEGER NOT NULL,
  overwritten INTEGER NOT NULL,
  failed INTEGER NOT NULL,
  done INTEGER NOT NULL,
  bytes_exact INTEGER NOT NULL,
  PRIMARY KEY (run_id, bucket, ts)
);

CREATE TABLE profiles (
  email       TEXT PRIMARY KEY,
  avatar      TEXT,
  -- Provenance for re-resolve/debug: 'upload' | 'url' | 'github' | 'gravatar'.
  avatar_src  TEXT,
  updated_at  INTEGER NOT NULL
, name TEXT);

CREATE TABLE "pyramid_multiscans" (
  dataset TEXT NOT NULL,
  tier TEXT NOT NULL,
  shard_dur TEXT NOT NULL,
  period_start INTEGER NOT NULL,
  period_end INTEGER NOT NULL,
  key TEXT NOT NULL,
  scans TEXT NOT NULL,
  encoder TEXT NOT NULL,
  digests TEXT,
  written_at INTEGER NOT NULL,
  PRIMARY KEY (dataset, key)
);

CREATE TABLE scan_runs (
  run_id       TEXT PRIMARY KEY,                -- the Batch job uid ($BATCH_JOB_UID); else `<scan>@<started_ts>`
  store        TEXT NOT NULL DEFAULT 'primary', -- 'primary' or a `STORES_JSON` key (a secondary store's own job)
  scan         TEXT NOT NULL,                   -- scan id (YYYY-MM-DD or YYYY-MM-DDTHHMM)
  kind         TEXT NOT NULL,                   -- the profile's job kind: scan | reproc | listing | static-names | …
  parent       TEXT,                            -- the orchestrating run of a downstream job (no FK: either may land first)
  status       TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'nop')),
  failed_phase TEXT,                            -- the phase running when it failed
  error        TEXT,                            -- the failure's one-line reason (exit code + command, or Batch's event)
  started_ts   INTEGER,                         -- epoch s: the job's task started (RUNNING)
  finished_ts  INTEGER,
  job_name     TEXT,                            -- the Batch job id (its name's last segment)
  region       TEXT,
  machine      TEXT,
  spot         INTEGER,                         -- 1 = SPOT provisioning
  tasks        INTEGER,
  image        TEXT,
  cost_usd     REAL,                            -- estimate: the profile's $/h for the machine × run hours × tasks
  gen          TEXT,                            -- the index generation the run wrote, if any
  source       TEXT NOT NULL CHECK (source IN ('job', 'backfill')),
  updated_ts   INTEGER NOT NULL
);

CREATE TABLE scan_run_outputs (
  run_id  TEXT NOT NULL REFERENCES scan_runs(run_id),
  key     TEXT NOT NULL,
  uri     TEXT NOT NULL,                        -- gs:// | r2:// | s3:// prefix or object
  bytes   INTEGER,
  objects INTEGER,
  rows    INTEGER,
  gen     TEXT,
  PRIMARY KEY (run_id, key)
);

CREATE TABLE scan_run_phases (
  run_id      TEXT NOT NULL REFERENCES scan_runs(run_id),
  phase       TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  started_ts  INTEGER,
  finished_ts INTEGER,
  status      TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('running', 'done', 'failed')),
  note        TEXT,
  PRIMARY KEY (run_id, phase)
);

CREATE TABLE session_log (
  sid         TEXT PRIMARY KEY,   -- per-tab session id (client `sessionStorage`)
  email       TEXT,               -- the request's identity (server-derived); NULL = anonymous public viewer
  via         TEXT,               -- session | grant | public
  build       TEXT,               -- the client's build SHA
  ua          TEXT,               -- User-Agent
  started_ms  INTEGER NOT NULL,   -- earliest event (client clock, epoch ms)
  last_ms     INTEGER NOT NULL,   -- latest event
  n_events    INTEGER NOT NULL,
  n_errors    INTEGER NOT NULL,   -- error | reject | console events, and api events with status 0 or >= 500
  n_batches   INTEGER NOT NULL,
  first_url   TEXT,               -- the first batch's first `start` / `nav` URL
  received_ts INTEGER NOT NULL    -- epoch s (server clock) of the latest batch: what retention keys on
);

CREATE TABLE session_log_batches (
  sid         TEXT NOT NULL,
  pid         TEXT NOT NULL,      -- page-load id (a reload keeps `sid`, gets a new `pid`)
  seq         INTEGER NOT NULL,   -- the batch's number within its page load
  received_ts INTEGER NOT NULL,
  first_ms    INTEGER NOT NULL,
  last_ms     INTEGER NOT NULL,
  n           INTEGER NOT NULL,
  events      TEXT NOT NULL,      -- JSON array of `{t, k, ...}`, as validated
  PRIMARY KEY (sid, pid, seq)
);

CREATE TABLE session_log_switch (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  until_ms    INTEGER,            -- NULL = off
  who         TEXT NOT NULL,      -- admin who last set it
  ts          INTEGER NOT NULL    -- epoch s of that change
);

CREATE TABLE stage_replies (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id    INTEGER NOT NULL REFERENCES plans(id),
  stager     TEXT NOT NULL,             -- lowercased identity of the batches' stager
  channel    TEXT NOT NULL,
  thread_ts  TEXT NOT NULL,             -- the parent it replies under
  slack_ts   TEXT,                      -- the reply; NULL while being posted
  open       INTEGER,                   -- 1 = taking this stager's stages; NULL = closed
  created_ts INTEGER NOT NULL,          -- epoch seconds
  updated_ts INTEGER NOT NULL           -- the last stage it took
);

CREATE TABLE stage_batches (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id    INTEGER NOT NULL REFERENCES plans(id),
  note       TEXT,                    -- the one memo for this gesture
  created_by TEXT NOT NULL,           -- the identity that trashed
  created_ts INTEGER NOT NULL         -- epoch seconds
, reply_id INTEGER REFERENCES stage_replies(id));

CREATE TABLE plan_items (
  plan_id  INTEGER NOT NULL REFERENCES plans(id),
  prefix   TEXT NOT NULL,             -- s3://<bucket>/<path>/ (trailing slash)
  note     TEXT,
  added_by TEXT NOT NULL,             -- CF Access identity that added it
  added_ts INTEGER NOT NULL, batch_id INTEGER REFERENCES stage_batches(id), as_of TEXT, kind TEXT CHECK (kind IS NULL OR kind = 'object'),
  PRIMARY KEY (plan_id, prefix)
);

CREATE UNIQUE INDEX access_log_dedupe ON access_log (event, session_sub, path, bucket) WHERE bucket IS NOT NULL;

CREATE INDEX access_log_grant ON access_log (grant_id, ts DESC);

CREATE INDEX access_log_ts ON access_log (ts DESC);

CREATE INDEX access_log_daily_day ON access_log_daily (day DESC);

CREATE INDEX access_log_daily_grant ON access_log_daily (grant_id, day DESC);

CREATE INDEX access_requests_created_at ON access_requests (created_at DESC);

CREATE INDEX access_requests_email ON access_requests (email, created_at DESC);

CREATE UNIQUE INDEX access_requests_one_pending ON access_requests (email) WHERE status = 'pending';

CREATE INDEX access_requests_status ON access_requests (status, created_at DESC);

CREATE INDEX admin_edits_tbl ON admin_edits (tbl, ts);

CREATE INDEX idx_deletion_bands_prefix ON deletion_bands (prefix);

CREATE INDEX idx_footer_cache_lru ON footer_cache (state, last_access_day, date);

CREATE INDEX grants_created_at ON grants (created_at DESC);

CREATE INDEX grants_disabled_at ON grants (disabled_at);

CREATE INDEX idx_index_row_groups_depth ON index_row_groups (date, variant, gen, d_min, d_max);

CREATE INDEX idx_index_row_groups_user  ON index_row_groups (date, variant, gen, u_min, u_max);

CREATE INDEX idx_og_tokens_minted ON og_tokens (minted_ts DESC);

CREATE INDEX idx_og_tokens_token ON og_tokens (token);

CREATE INDEX pending_auth_email ON pending_auth (email, created_at);

CREATE INDEX pending_auth_ip ON pending_auth (ip_hash, created_at);

CREATE UNIQUE INDEX pending_auth_token ON pending_auth (token_hash);

CREATE INDEX idx_scan_runs_parent ON scan_runs (parent);

CREATE INDEX idx_scan_runs_scan ON scan_runs (store, scan);

CREATE INDEX idx_scan_runs_started ON scan_runs (store, started_ts);

CREATE INDEX idx_session_log_email ON session_log (email, last_ms);

CREATE INDEX idx_session_log_received ON session_log (received_ts);

CREATE INDEX idx_session_log_batches_received ON session_log_batches (received_ts);

CREATE UNIQUE INDEX stage_replies_open ON stage_replies (plan_id, stager) WHERE open = 1;
