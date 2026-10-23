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
| 10-22T1545 (manual) | 30 | 46 | 107 | 24 | 44 | 289 (green) |
| 10-22T2316 (manual) | 21 | 48 | skipped (compaction held the store) | 15 | 41 | 157 (green; marker failed, probe refused search) |

Phases overlap (static-names runs beside interval-store and post), so the totals aren't sums. The total has grown from ~1 h to ~4.6 h in two weeks.

**Failures, 10-09 to 10-22**

| date | phase | cause | fix |
|---|---|---|---|
| 10-18 | head job | Batch `maxRunDuration` 6 h killed the job, skipping its exit trap; the run stayed `running` | cap 12 h; `scan-run reconcile` + `/scans` `LOST_S` (5c226585) |
| 10-21 | retire | its verify downloads the whole served interval-store generation (~120 GB) onto a 50 GB boot disk (exit 1, ENOSPC, which then failed every later post step); the re-run's DuckDB was set to 80 GB on a 32 GB VM (exit -9) | post `e2-highmem-16` + 400 GB (3cbb09a4, 6515673e); `verify_budget` caps DuckDB at 3/4 of RAM (117a31cd); retire always cleans its work dir (937db51b). Green on 10-22 |
| 10-22 | interval-store | `prepare` wrote under the pre-compaction generation (`follow` bug) | 599893e7 |
| 10-22 | static L3 carry | drill merge spilled 1.2 TiB of DuckDB temp | chunked by `q` range (f4653462) |
| 10-22 | cf-usage | relative `site/wrangler.toml` outside `/app` | 58f4ccb7 |
| 10-22 | interval-store marker | the append's exact check found 196,021 depth-3 objects with no `generation` in the base's range files, lost by the 10-09 `backfill-generation` (the served sorts have them, so the old check passed) | in progress: backfill RC + the append fills a NULL generation |
| 10-23 | static compaction | the laptop driver died on a connection reset and released the coordinator lease while the compaction ran; the 07:00 append would have superseded it | lease held by hand; in progress: a running compaction holds the store itself |
| 10-08…10-22 | static compaction | drill fold ran 6 h silent and was killed; level 6 stopgap | chunked fold (70ed412a); re-trial running |

## What each phase scales with

| phase | scales with | the O(#changes) path |
|---|---|---|
| list | fleet objects (a full GCS listing per bucket) | GCS has no listing diff. Options: Storage Insights inventory reports, or Pub/Sub object-change notifications feeding a change log; either makes the scan's input the day's changes |
| path-index | fleet objects: full per-scan sorts | the interval store already serves the Map; `specs/lean-scans.md` retires per-scan sorts. What still reads them: `dups`, `attr.tsv`, `index-rows`, the digest. Move each onto the interval store or the change set, then drop the step |
| static-names append | the per-range append already reads the previous scan's `state/<prev>/copen` (flat, ~7 min); the **catalog** step expanded every scan's `cdelta` since the base (~29 min at 26 scans, growing ~1 min a scan); drill ∥ anchors read O(log) run tiers (anchors 40–90 min, the long pole) | the catalog carries its candidate counts (`xcount`) and reads the runs' shards for history (`specs/append-open-state.md`, built); carries are already amortized O(changes·log) |
| static-names compaction | corpus size, every 2^L scans; today one Python core in `merge_shards` and a slow drill fold | parallel `merge_shards` (in progress), chunked drill (70ed412a), local-SSD staging, parallel uploads; floor ~30–45 min on one VM for a ~550 GiB corpus |
| interval-store append | already carried: `ranges` reads `state/<prev>/` (11 min), publish 3.5, carries 7 (10-22T1545) | none needed on Batch |
| retire | fleet size: its verify re-downloads the served generation (~120 GB, ~8 min) and compares a whole scan (440M objects, 22 min); plus D1 footer rows | the interval-store append writes the marker (item 4, in progress); retire then only lists, backs up and deletes. D1 deletes stop once `index-sync -R` has run 2 scans |
| dups, index-rows, warm-cache | fleet-sized reads per scan | revisit after path-index is gone |

## Plan, in order

1. **Green and bounded first.**
   - Land the parallel `merge_shards`.
   - Finish the static compaction re-trial, set `compact_level` back to 5, and unpin prod's `STATIC_GEN`.
   - A manual scan on the new image, which carries cw's listing `version` column.
2. **Compaction under an hour:** local-SSD staging, parallel uploads, and overlapping the independent stages. Measure each stage against the floor above.
3. **Appends O(#changes):** the static catalog's carried counts (`specs/append-open-state.md`; the per-range open versions were already carried on Batch in both stores). Next on that path: anchors.
4. **Retire's verify moves into the interval-store append** (retire becomes markers-only: constant cost). Implemented; not deployed yet. Design and rollout below.

## Item 4: the parity marker comes from the append

**What a marker certifies.** `scan retire` deletes a scan's per-scan files only when `interval-store/<gen>/verify/<id>.json` passes (found through compactions and backfills). Until now `interval-store verify` wrote it at retire time: about 20 sampled views and diffs, plus an exact comparison of every object's `generation`. Now the append writes it, and it compares every row: for every key range and each version table (`pvl`, and the owner slices `sv`, `svt`), the store's answer at the scan equals the scan's rows. That is a stronger check than the sampled views. Markers from `interval-store verify` still count, and retire without `-m` still makes one for a scan that has none.

**Options considered.**
- (a) **In the append, against the published store (chosen).** Each range task already holds the scan's rows for its range (`np`, `ns`) and has just written the run's deltas. The check adds one read per range: the base's range files for that range (`pvl/`, `sv/`, `svt/` `r####.parquet`, through the mount) and each run's delta for that range, as listed in the newest earlier manifest. It combines them as the reader does (one row per version, smallest `vt`, `vf ≤ D < vt`) and compares them row for row with the scan (`EXCEPT ALL` both ways, over the `HASH` columns: the change columns, `dr` from the stored `wts`/`wb`, `last_read`, `tot`). The check reads the published files, not the scratch `state/` chain that the append's own reconstruction check uses, so a corrupted base or run file, a merged run, or a lost close shows up as missing or extra rows. `publish` then compares each of the run's served sorts row for row with the deltas they were cut from. The marker is the sum over all ranges plus the cut check.
- (b) In `index`: the per-scan sorts are there, but the store's answer at the scan isn't. That would need the same reads (a) makes, with a second copy of the logic, and `index` runs before the append. Rejected.
- (c) Range-read the served sorts' needed groups in retire: still a whole scan's worth of groups (every range is touched), on the post VM, every day. Rejected in favour of (a), which reuses inputs the append already has.

**Exact.** Every row of every table is compared; nothing is sampled or hashed (`check_tables`, `check_cut` in `interval_append.py`). `pass` needs rows > 0, zero missing and extra in every table and every served sort, and (where the generation carries it) every object's `generation` equal. No marker is written when a range or the cut wasn't checked (scans appended before this change), or when a range was checked against different runs than its manifest lists (a carry's revision landed between `ranges` and `publish`). `retire -m` keeps such a scan, and a retire without `-m` verifies it the old way.

**What it assumes.** The marker vouches for the store as published with the scan's manifest. Later carries and compactions rebuild runs and bases from the same versions. Compactions were already trusted this way (markers are found through `compaction.json` lineage). A carry's merge is the same min-`vt` combine the check uses (`test_the_check_over_merged_runs_finds_every_row_equal`). Base served sorts are cut once per generation (tested byte for byte against a fresh build). The check reads range files, not the served base sorts.

**Cost.**
- Per range, the extra work is reading the base's three range files (gcs: ~18 + 16 + 17 GB over 256 ranges, ~200 MB on average, ~1.5 GB at the largest), the runs' deltas (small), a window over the base's versions, and two `EXCEPT ALL`s over the range's ~1.7M live rows per table. Estimate: +10–30 s on a range whose append takes ~60 s, so the ranges stage grows ~3–5 min of wall time (16 tasks × 16 ranges).
- The cut check in `publish` compares ~11M rows × 5 sorts on a busy day: about a minute.
- Each range doc records the check's seconds (`check.s`), and the run's `meta.json` records the cut check's (`check_s`). The first manual scan measures these.
- Retire does no verification. It lists, copies backups server side, deletes D1 rows, then deletes files: estimated a few minutes, down from 37–44.

**Status.** `[cloud]` code and tests are on the agent branch (see the commits). Rollout:
1. Rebuild the `:latest` image and run a manual scan. Its append writes `verify/<scan>.json` (`how: append`); check that it passes, and read `check.s` / `check_s`.
2. No profile change is needed. Retire verifies only scans that have no marker. The scans appended before the rebuild (10-22T0702, 10-22T1545, and any daily before it) retire the old way, once each. Every later scan goes markers-only.
3. `options.retire.markers_only` stays off, as a fallback. A scan that ends up without an append marker (a carry's revision raced its publish) is verified the old way instead of being kept forever. That rare case is why the post VM keeps its 400 GB disk for now. If it never happens, set `markers_only: true` and shrink the disk; the VM keeps its memory for `dups`.
5. **Retire path-index:** move its remaining readers, then drop the step (`specs/lean-scans.md`).
6. **Listing:** evaluate inventory reports vs change notifications for an O(#changes) input. This is the last O(fleet) term.

Every change is verified with a manual scan before the next daily (`verify-with-manual-scan`). Phase minutes are tracked in the table above.
