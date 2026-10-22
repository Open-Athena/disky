# gcs scan pipeline: fast, robust, O(#changes)

Status: tracking doc, opened 2026-10-22. This is the top priority for gcs (Ryan, 2026-10-22). The aim is a daily scan whose time and cost track what changed in the fleet, not the number of scans or the size of the fleet, and that finishes green without hand repair.

## Where it stands (daily 07:00 runs, minutes per phase, from `/scans`)

| scan | list (max bucket) | path-index | static-names | interval-store | retire | total |
|---|---:|---:|---:|---:|---:|---:|
| 10-09 | ~25 | 42 | 0 | 0 | 0 | 61 |
| 10-13 | 28 | 35 | 78 | 11 | 0 | 148 |
| 10-16 | 32 | 40 | 63 | 12 | 0 | 188 |
| 10-19 | 19 | 45 | 262 | 28 | 0 | 344 |
| 10-21 | 20 | 50 | 102 | 12 | 7 (failed) | 202 (failed) |
| 10-22 | 30 | 45 | 129 | 3 (failed) | 37 | 277 |

Phases overlap (static-names runs beside interval-store and post), so the totals aren't sums. The total has grown from ~1 h to ~4.6 h in two weeks.

**Failures, 10-09 to 10-22**

| date | phase | cause | fix |
|---|---|---|---|
| 10-18 | head job | Batch `maxRunDuration` 6 h killed the job, skipping its exit trap; the run stayed `running` | cap 12 h; `scan-run reconcile` + `/scans` `LOST_S` (5c226585) |
| 10-21 | retire | exit 1, then exit -9 on the post VM | open: RC needed |
| 10-22 | interval-store | `prepare` wrote under the pre-compaction generation (`follow` bug) | 599893e7 |
| 10-22 | static L3 carry | drill merge spilled 1.2 TiB of DuckDB temp | chunked by `q` range (f4653462) |
| 10-22 | cf-usage | relative `site/wrangler.toml` outside `/app` | 58f4ccb7 |
| 10-08…10-22 | static compaction | drill fold ran 6 h silent and was killed; level 6 stopgap | chunked fold (70ed412a); re-trial running |

## What each phase scales with

| phase | scales with | the O(#changes) path |
|---|---|---|
| list | fleet objects (a full GCS listing per bucket) | GCS has no listing diff. Options: Storage Insights inventory reports, or Pub/Sub object-change notifications feeding a change log; either makes the scan's input the day's changes |
| path-index | fleet objects: full per-scan sorts | the interval store already serves the Map; `specs/lean-scans.md` retires per-scan sorts. What still reads them: `dups`, `attr.tsv`, `index-rows`, the digest. Move each onto the interval store or the change set, then drop the step |
| static-names append | scans since the base: every append refolds the base's `cintervals` + every run's `cdelta` (`rebuild_open`), and its carries' cost grows by level | keep each run's open-version state (`copen`) so an append reads the previous state + the day's delta only; carries are already amortized O(changes·log) |
| static-names compaction | corpus size, every 2^L scans; today one Python core in `merge_shards` and a slow drill fold | parallel `merge_shards` (in progress), chunked drill (70ed412a), local-SSD staging, parallel uploads; floor ~30–45 min on one VM for a ~550 GiB corpus |
| interval-store append | same refold pattern as static-names | same fix |
| retire | D1 rows deleted (footer rows) | drops once `index-sync -R` (no D1 footer rows) has run 2 scans; RC the 10-21 failure |
| dups, index-rows, warm-cache | fleet-sized reads per scan | revisit after path-index is gone |

## Plan, in order

1. **Green and bounded first.**
   - RC the 10-21 `retire` failure (exit 1, then -9).
   - Land the parallel `merge_shards`.
   - Finish the static compaction re-trial, set `compact_level` back to 5, and unpin prod's `STATIC_GEN`.
   - A manual scan on the new image, which carries cw's listing `version` column.
2. **Compaction under an hour:** local-SSD staging, parallel uploads, and overlapping the independent stages. Measure each stage against the floor above.
3. **Appends O(#changes):** carry the open-version state between appends in both stores (static-names and interval-store).
4. **Retire path-index:** move its remaining readers, then drop the step (`specs/lean-scans.md`).
5. **Listing:** evaluate inventory reports vs change notifications for an O(#changes) input. This is the last O(fleet) term.

Every change is verified with a manual scan before the next daily (`verify-with-manual-scan`). Phase minutes are tracked in the table above.
