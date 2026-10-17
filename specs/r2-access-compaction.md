# R2 access aggregates: compaction

Status: 2026-10-17, proposed. Follows [r2-access-logging.md] (the hourly collector, `infra/cf/r2-access`).

## Why

At OA `marin-na` (~80K events/hour), the collector stores two things:

| what | where | format | size | retention |
|---|---|---|---|---|
| raw API pages | `runs/<scheduled>/<run>/<bucket>/<hour>/NNNN.json` | JSON (telemetry events, incl. IPs/actors) | ~1.7 GB/day | 30 days (lifecycle), ~51 GB standing |
| hour aggregates | `agg/<bucket>/<day>/<HH>/<run>.json` | JSON, uncompressed: `[key, action, n, first_ts, last_ts]` rows | ~5.2 MB/hour | forever: ~46 GB/year |

Nothing reads the raw pages: re-drains query the provider (which keeps 7 days), not the archive. The aggregates are the durable product, and they are stored at their least compact:

- every row spells the full object key as a JSON string, and marin keys are long paths with shared prefixes;
- action names repeat as strings, and timestamps as 13-digit ms integers;
- hours are disjoint in time but not in keys: a hot key appears in every hour it's read.

The consumer also re-reads all history on every scan: the demo's daily ingest downloads every public hour file and runs `dt-cloud access r2-agg` over all of them, O(history) per scan rather than O(new hours).

What the consumer needs (`path-index -x`): per directory, the epoch **day** of the latest GET/HEAD and the GET/HEAD counts; per bucket, the observation window. Minute precision and per-hour splits matter only while an hour can still change (within `RETENTION_HOURS`).

## Plan

### 0. Measure

Pull one OA day of `agg/` (24 hour files, via the r2 session; the archive is private) and record, for that day:

- the raw JSON size, gzip -6 and zstd -3 of each hour;
- one daily parquet (zstd, dictionary, sorted by key) with one row per `(key, action)`: sum `n`, min `first_ts`, max `last_ts`.

Then fill in the table below before building steps 2–3. These are expected sizes, not measured ones:

| form | per day | per year |
|---|---|---|
| hourly JSON (today) | ~126 MB | ~46 GB |
| hourly JSON, gzip | ~15–25 MB? | ~5–9 GB? |
| daily parquet, rolled up | ~2–5 MB? | ~1–2 GB? |

### 1. Raw pages: 7 days

Set `retentionDays: 7` on the OA stack (the program's floor). That matches the provider's own retention, so a raw page outlives every hour it could re-derive. It cuts ~40 GB standing at OA. Needs a Pulumi apply.

### 2. Gzip the hour aggregates in the Worker

Write `agg/…/<run>.json.gz` through `CompressionStream('gzip')` (native in Workers, no dependency); the state's `agg` key carries the suffix and readers branch on it. Old `.json` aggregates stay readable. The public hour files stay plain JSON: they hold only aggregate rows, are small, and are read by the demo's CI.

### 3. Daily rollup in the consumer job

A new `dt-cloud access r2-roll` writes, for each UTC day whose 24 hours are all past `RETENTION_HOURS` (final, never re-drained again), one `access/r2-days/<bucket>/<YYYY-MM-DD>.parquet`:

- one row per `(key, action)`: `n` (sum), `first_ts` (min), `last_ts` (max), `sampled` (any hour sampled), all sorted by key;
- zstd, dictionary-encoded key and action;
- the day's capture totals (`observed`, `estimated`, `excluded`, `max_sample_interval`) in the parquet metadata.

It is IDP: a day already rolled is skipped, and a re-roll produces byte-identical output.

`r2-agg` then reads the day parquets plus only the hour files after the last rolled day. Each scan costs O(new days) of rolling plus a DuckDB scan over small parquets.

A later step can roll closed months the same way, if per-day files ever add up.

### 4. Retire rolled hours

After a day's parquet is written and read back, the job deletes that day's hour aggregates (private `agg/`) and public hour files. The collector's `state.json` `frozen` map stops growing past the rolled point (open question below).

## Open questions

- **Who owns the cursor?** The job deletes rolled hours, but the collector owns `state.json`. Either the collector stops recording `frozen` hours (they are never re-drained, so it needs nothing from them), or the job keeps its own `rolled_through` manifest beside them. I lean toward the first: `frozen` exists only for bookkeeping.
- **Where OA's job runs.** The demo rolls in its GHA ingest. OA `marin-na` has no consumer wired yet: the r2 deployment's job would read the private archive with its own key.
- **Day granularity.** The read lens shows days. If a finer lens ever matters, keep `last_ts` at the minute in the day parquet (it already is) and only lose per-hour counts.

## Not in scope

GCS's access pipeline (usage-log CSVs → layer-2a) has its own retention (`usage-log-ingest-cron`) and is unchanged.

[r2-access-logging.md]: r2-access-logging.md
