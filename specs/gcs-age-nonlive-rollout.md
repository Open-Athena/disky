# gcs: age history and non-live bytes (rollout)

Two shared features run on hf but not on gcs: the change-interval age history ([age-store]) and the non-live bytes line ([non-live-bytes]). Under deployment parity, gcs runs both. This spec is gcs's rollout: the config, its order, what each scan does differently, and how to check it. Both features are in the deployed site (`gcs-prod` contains `16a87445` and `bff2339e`). Neither needs a D1 migration: the age store lives on R2, and its retirement deletes rows from the existing `index_schema` / footer tables with `index_footer.d1_delete`.

## What gcs is missing today

| | primary (`gcs.yml`) | meta (`gcs-meta.yml`) |
|---|---|---|
| per-scan age pyramids (`options.index.age_pyramids`) | **never produced**: no `age-pyramid-*.parquet` under `listing/`, so `/api/age-pyramid` answers empty and the Map's age chart never mounts | produced every scan (`meta-l2/<scan>/index/<gen>/`) |
| `age-store` step + site `AGE_STORE_PREFIX` | no | no |
| `non-live` step (`soft-deleted`) | no | no |

So on the primary, age-store has nothing to store until the pyramids exist. Turning them on is the larger change: it restores the age chart (the age-index feature, never shipped on gcs) and adds per-scan work on the index task. The meta store has the inputs already.

The marin buckets and the data bucket all have 7-day soft delete. The data bucket's soft-deleted bytes are dominated by the primary's own `retire` (each old scan's path sorts and raw listing shards, deleted daily), so the meta store's non-live line shows the deployment's own double-billing window.

## Phase 1: meta store (commit `gcs meta store: non-live and age-store`)

Config: `gcs-meta.yml` `steps: [non-live, age-store]`, `options.non-live.classes: [soft-deleted]`, `options.age-store: {store: r2://oa-gcs-usage-index/history/meta-age, gen: 2026-10-23meta-age, keep: 2, execute: true, copies: [r2://oa-gcs-usage-index]}`; `site/wrangler.toml` adds `"AGE_STORE_PREFIX":"history/meta-age"` to the meta store's `STORES_JSON` vars (prod and preview).

Order:
1. Deploy the site (`site/deploy --dev`, check, then prod). Safe before any store exists: `readAgePoint` returns null without a current generation, and the reader keeps using per-scan pyramids.
2. Rebuild the job image (`job/build.sh`); the profile is read from the image (`/app/job/profiles/gcs-meta.yml`).
3. Fire a manual meta scan (`job/meta-submit.sh`), not the next cron. Its `post` task:
   - `age-store` bootstraps `2026-10-23meta-age` from every meta scan that still has pyramids (about 26 scans, under 1,000 small files). It verifies every scan's cells as an exact multiset before it publishes manifests and `CURRENT.json`, then retires pyramids of covered scans older than the newest two, clearing their D1 age rows (`-T meta`) first. Minutes.
   - `non-live` (in the `pre` task, beside `list`) lists the data bucket's soft-deleted objects (a minute or two at the current churn), writes `meta-l2/<scan>/non-live/oa-gcs-usage-dvx.parquet` and merges `snapshots/meta/<scan>/non-live.json`. The R2 source is skipped (no soft delete).
4. Verify: the run's `age-store` log line (JSON: `added` with `cells`, `retired` per root); `gs://…/history/meta-age`'s twin on R2 holds `interval-store/CURRENT.json` and `verified/<scan>.json` per scan; `/meta` age chart at the oldest and the newest scan answers the same as before the retire (`/api/age-pyramid?store=meta&date=<scan>&path=`); `/meta`'s root view shows the "Not in this view" line.

Each later meta scan appends one run (changed cells only) and retires one scan's pyramids.

## Phase 2: primary non-live (commit `gcs primary: non-live`)

Config: `gcs.yml` `steps += non-live`, `options.non-live.classes: [soft-deleted]`. No site change (the root view reads `snapshots/<scan>/non-live.json` through `/data`, and `publish-r2 -S` copies the snapshot dir to R2).

What the next scan does differently: a `pre` task (`machines.pre`, n2-highmem-8) lists each marin bucket's soft-deleted objects, one bucket after another, at one list call per 1,000 objects. Its cost is the soft-deleted population of the last seven days, which tracks deletions, not the bucket's size. Measured from a laptop on 2026-10-22 (`gcloud storage ls --soft-deleted`, the same API): about 5,000 objects a second per bucket, one to five million soft-deleted objects in each of the four busier buckets, about eleven million in all; listed one bucket after another, that is about 45 minutes (the largest bucket alone took 18).

Risk: `index` waits for every `pre` task (`scan.nf`: `pre` is gathered with `list`). Today the longest `list` task takes about 32 minutes, so on an ordinary week the serial listing already holds `index` back by 10 to 15 minutes, and a sweep week (tens of millions of objects soft-deleted at once) would hold it for hours. Before enabling it on the primary, a `[cloud]` change should take `non-live` off the index's path. Either:
- run it as its own DAG branch that joins at `publish-r2` (its only consumer is the snapshot's `non-live.json`), or
- list the buckets concurrently (one thread per source) and bound the step with a deadline after which it writes what it has as a partial row.

Hold Phase 2 until that change lands; it is committed separately so it can be held. Concurrent listing alone brings it to the largest bucket's time (about 18 minutes), inside the `list` window.

Verify: the run's `non-live` lines (`non-live <bucket> soft-deleted: … B in … , expiring by …`), `listing/<scan>/non-live/<bucket>.parquet` (6 files, one row each), `snapshots/<scan>/non-live.json` on GCS and R2, and the root view's "Not in this view" line on dev.gcs.oa.dev, then gcs.oa.dev.

## Phase 3: primary age (branch `gcs-age-primary`, held)

Config: `gcs.yml` `options.index.age_pyramids: true`, `steps += age-store`, `options.age-store: {store: r2://oa-gcs-usage-index/history/primary-age, gen: <first scan>-age, keep: 2, execute: true, copies: [r2://oa-gcs-usage-index]}`; `site/wrangler.toml` `AGE_STORE_PREFIX = "history/primary-age"` in `[vars]` and `[env.preview.vars]`. `index-sync` already syncs every variant a generation holds, so the pyramids get pointers and cold footers with no profile change.

Prerequisites (shared code, the root session's `cloud`):
1. Cherry-pick hf's `c03b6878` (`[cloud] Republish verified files when retrying an unpublished age history`) to `cloud` and merge it here: without it, a retried first publication can leave stale same-length files.
2. Make `age-store`'s DuckDB size configurable (`options.age-store.threads` / `mem`, passed to `cells`, `verify` and the interval build/add). Today it is fixed at 2 threads / 2 GB, which suits hf (about 270K cells a scan) but not gcs.

Measurement before enabling (one scratch Batch job, n2d-highmem-32 with local SSD, nothing published):
1. Stage the two path sorts that still exist (`listing/2026-10-21T1439/index/20261021T144044Z/path-index.parquet`, `listing/2026-10-22T0702/index/20261022T070220Z/path-index.parquet`).
2. `dt-cloud age-pyramid -c -m 100GB -t 32 -o <dir>/<scan> <path sort>` for each: wall time, peak RSS, rows per tier (the floor is 2^27 bytes at gcs's size; the 2026-10-22 `bysize` footers put about 7M rows, objects and directories, over it, so the kept directories are at most that many and the finest tier is likely tens of millions of cells).
3. `dt-cloud age-store -g scratch-age -L '<dir>/{id}/age-pyramid-1h.parquet' -s <local root> -o <local scratch store> -w <work>` (no `-r`: nothing retired), once per scan: the base build, then one append, with their times and the store's size.

Go if the pyramid adds no more than ~10 minutes to `index` (it is on the critical path, and fatal there: `age-pyramid -c` failing fails the scan) and the append fits the `post` task's machine (e2-highmem-16, 400 GB). Otherwise the pyramid needs its own soft task off the critical path first (`[cloud]`).

Rollout once those hold:
1. Deploy the site with `AGE_STORE_PREFIX` (safe before any store exists, as in Phase 1).
2. Merge `gcs-age-primary`, set `gen` to the first scan's date, rebuild the image, fire a manual scan. Its `index` writes the eight pyramids beside the sorts; `index-sync` points at them; `post`'s `age-store` builds the base generation from that one scan and verifies it.
3. History starts at that scan. Earlier scans have no pyramids; the age chart stays empty for them, as today. A backfill is possible (each old scan exported from the path interval store, `interval-export`, then `age-pyramid`, then one `age-store` build over all of them in scan order) but it is about a hundred full-size exports and a build whose input is every scan's cells: a separate decision, not part of this rollout.
4. Verify: the scan's `verification` of the pyramids (`-c`), the `age-store` JSON line, `history/primary-age/interval-store/CURRENT.json` on R2, the Map's age chart at the root and a few drilled paths on dev, then prod; a second scan's append adds only changed cells.

## Cost growth

Neither feature adds work that grows with the number of scans:
- `non-live` lists only soft-deleted objects (the last seven days of deletions) and writes a few rows per bucket per scan.
- `age-store` reads the current scan's pyramids (the scan's state), appends changed cells (the changes), and verifies the new scan (its state). Each run mirrors the store's current generation: the base plus at most a logarithmic number of runs, compacted every 32 scans, so it is bounded by state plus recent changes. Its retirement checks only scans that still hold pyramids (the newest two after the first run). The per-scan ledger (`verified/<scan>.json`, the manifest) is a few hundred bytes a scan.

What is per scan and proportional to state, not changes: the pyramid build (one pass over the path sort, like the rest of `index`) and the age-store's cell build and verification. Both are the same order as the scan's existing full listing and index.

[age-store]: age-store.md
[non-live-bytes]: non-live-bytes.md
