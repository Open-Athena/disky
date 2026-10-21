# Static name index: compaction into a new generation

Status: building (2026-10-21, Ryan's go). The design below was approved 2026-10-09 and half-built on branch `compaction` (`static_compact.py`, off the pre-rework drill); that branch is reference, not a merge source. This revision adds a shared compaction **policy** (both stores) and a **builder read source** toggle.

## Why it's needed now (gcs, 2026-10-20/21)

- The static index grows ~12.7 GB per scan (one copy; ×2 with R2), ~8× the interval store. gcs found this is real change volume, not merge bloat: each changed path is indexed under many substrings, and newly indexed substrings carry their full history (10-20: 124M of the run's 639M rows were 1,192 new substrings' histories).
- Every append re-reads every earlier scan's `cdelta/` since the base (`static_merge.FOLDED_RETAIN`), so per-scan work and retained files grow with the scan count. Past the compaction level, carries stop and runs accumulate (`append_runner.plan_carries`); `compaction_due` only logs.
- Compaction is the reset for both: a new base equal to a full rebuild, after which the old base, its runs and their retained `cdelta/`s can go (`store-gc`'s grace). It does **not** cut the per-change substring volume; that is a separate design question (fewer indexed substrings), tracked outside this spec.

## Compaction policy (shared: `append_runner`, both stores)

Before: `compact_level` L (every 2^L scans past the base; `none` = unbounded cascade). Now a policy object (`append_runner.Policy`) in the profile (`compact:`; `compact_level` stays as shorthand for `{level: L}`; `Profile.policy` is the one in force), evaluated in one place and used by `interval-store add`, `static-names add` and the staged runners' merge stage (`Runner.carries`, both stores):

- `append_runner.compaction_why(runs, policy, drilled, *, base)` → the reason (naming the rule that fired) or None; `compaction_due(…)` is `why is not None`. `runs`: the newest manifest's; `base`: the base's scan ids (read only by `every …d` and `geometric`). The signature keeps `compaction_due(runs, level)`'s positional order (a bare level / None still works), so the interval store's tests run unchanged; the 10-21 draft's `compaction_due(policy, manifest, now)` was reshaped to that (no clock: `every …d` measures scan ids against each other).
- `{level: L}`: as before (two runs at L − 1, carried); carries stop below L (`Policy.carry_level`).
- `{every: "<N>d" | "<N>s"}`: due when the newest scan is ≥ N days after the base's newest (scan ids' instants, `scan_id.scan_time`), or ≥ N scans are past the base. Carries run unbounded below it.
- `{geometric: r}` (exp-backoff): due when the scans past the base ≥ r × the base's own scan count (r = 1 doubles: compact at 2, 4, 8, … × the first base). Each compaction rewrites the whole base, so geometric spacing keeps its amortized cost per scan constant. Carries unbounded.
- `none`: never.
- Forms: profile JSON `{"level": 5}`, `{"every": "7d"}`, `{"geometric": 1}`, `"none"`; env `STATIC_NAMES_COMPACT` / `INTERVAL_STORE_COMPACT` and `add -C` take text: `5` (a bare level), `level:5`, `every:7d`, `every:30s`, `geometric:1.5`, `none` (`parse_policy`; `-C` also reads `$INTERVAL_STORE_COMPACT_LEVEL` as before).
- `-K/--compact-now` on `add` (both stores) forces one (when the generation has runs): reason `forced (-K)`.

Which rule fired and why: `add`'s log line and JSON summary (`compact.why`); the generation's `compaction.json` (`{"level": L}` exactly as before under a level, else `{"level": null, "rule": "every:7d", "why": …}`); the staged merge stage's log (`merge: level L is due: …` as before, else `merge: compaction due under <policy> (<why>): …`) and `Runner.compaction` (the reason, for the job's record; wiring it into `/scans` is `scan-run`'s, not done here). The merge jobs get the carry bound (`carry -L <Policy.carry_level>`: `none` under `every` / `geometric`).

## Built so far

- Phase 1 (policy): above.
- Phase 2a: the shared orchestrator (`append_runner.compact_store` + `Compactor`; `next_gen`, `current`/`set_current`, `chain`, `resolve_gen`, `carry_markers`, `prune_predecessors`, `compaction_doc`, `r2_publish_gen`, lifted from `interval_compact` / `interval_local.compact`, which now delegate: the interval store's tests pass unchanged), the static fold (`static_compact.fold_gen`), `static-names add` compacting when due (`-C`, `-K`, following `successor.json`), `static-names compact`, `build -r ranges.json` (the reference), `store-gc` keeping forwarding records. `test_static_compact.py`: every flavor, md5-equal to the rebuild.
- Phase 2b: the Batch `compact` stage in both runners (`Runner.compact_stage` after the merge stage, when the policy fires or `runs add -K` / `interval-store append -K`): one job, `static_merge compact` / `interval_append compact` (`append_runner.run_compaction`: follow successors, due check, `compact_store` reading the generation in place on the bucket's mount, then with `-r` `r2_publish_gen`: data, liveness files, `compaction.json`, the old generation's `successor.json`, then `CURRENT.json` last; predecessors pruned on GCS only, after the grace, for the interval store); when the stages' account isn't the R2 account a second job `compact-r2` as the R2 account (`publish_r2_chain`). Each runner's next run follows `successor.json` (`Runner.follow`: the profile needs no change). Non-fatal, waited on up to `-w` like the merge job.
- Phase 3 (`read_from`): below, once built.

## Builder read source (`read_from: gcs | r2`, both stores)

Both copies stay. A profile option (env override; CLI `--read-from`) picks which copy the builder reads earlier runs / the base from during `add`, merges and compaction. Writes still go to both (GCS, then the R2 copy). Each stage logs its read bytes and seconds per source, so a week of each gives real timings before deciding on one copy (gcs estimate: R2-only adds ~20 min to the pipeline). Default `gcs` (today's behavior).

## Reference design (2026-10-09)

## Why (original)

A generation (`static-names/<gen>/`) plus its runs answers every scan, but each run adds a tier to every read. The binary counter bounds that at ⌊log₂ n⌋ + 1 tiers. Close records also inflate the summed extents (light) and the stored rows (drill) until a merge collapses them. At level 5 (32 scans; for `2026-10-08c` before about 2026-11-08), or on demand, the base and its runs fold into a new base generation. That generation must be **the generation a full rebuild through its last scan would produce, byte for byte**: same files, same plans, same aliases. Readers then switch to it, and the old one stays until Ryan deletes it.

## What a generation holds, and how each part folds

`G` is the base generation, `runs` its live runs (oldest first, from the newest manifest), and `G'` the new generation, named `<last scan id>c` (e.g. `2026-10-09c`). A full rebuild through the last scan, with `G`'s key ranges, is the reference.

| Part of `G'` | Fold | Equal to the rebuild because |
|---|---|---|
| `scans.json`, `ranges.json` | `G`'s scans + every run's (`scans.json` of each scan's run), `G`'s ranges | the rebuild is defined over `G`'s key ranges (the ranges partition keys; they only cut files) |
| `cintervals/r####.parquet` | per range: `G`'s versions with every run's `cdelta` (close records set `vt`, opens add versions), `pyrmts.runs.merge_parquets` on `(depth, path, usr, vf)`, smallest `vt`, `INTERVAL_RG` groups | the coalesced versions through D are a function of the scans (`test_coalesced_from_scans_equals_two_step`); appending equals rebuilding (`test_append_open_equals_rebuild`) |
| `chist/r####.parquet` | `hist_sql` over each folded range | the same function of the same rows |
| `shards.json` | `plan_shards(chist, G's target_rows, G's task count)` | the build's planner over the same histograms |
| `sx/`, `sidecar/`, `sidecar.parquet` | `merge_shards([G, *runs], plan=G'`'s plan) per shard: the streaming k-way merge of each tier's rows in the shard's prefix range, smallest `vt` | `test_compaction_equals_full_build` |
| `catalog/{cells,index}.parquet` | `merge_catalogs([G, *runs])` | `test_catalog_equals_rebuild` |
| `catalog/meta.json`, `members.json`, `members.parquet` | recomputed from the folded catalog (headers ≥ 3 characters, their range rows, the shard of `G'`'s plan holding their first three characters) | the members are the catalog's headers; a member's shard is its prefix's shard |
| `drill/` | below | |

### The drilldown

A rebuild's drill is a function of the roots, and the roots of `G` ⊕ runs are exactly a rebuild's (`test_roots_equal_rebuild`). So the fold reads roots instead of recomputing them from the suffix shards (the costly level loop), and recomputes everything derived from them with the build's own code:

- **Classes.** A rebuild aliases members with equal root-set digests, the canonical being the least member. The digest is two order-free sums of row hashes, so it folds additively. `G`'s per-member digests (`drill/digest/`), plus each run row's hash, plus for a close record its row's hash with `vt` = D less the same row's hash with `vt` = OPEN. That gives every member's digest through D, and the classes follow as `alias-plan` makes them. This includes members new in the runs, whose classes the runs keep as singletons.
- **Long roots and rollups, per shard of `G'`'s plan.** For the shard's canonical members, take `G`'s rows of each one's canonical in `G` (from `G`'s `long/roots/`, row groups pruned by their `q` statistics), combine them with the runs' rows (via each run's own canonical), then `build_roots`. That gives roots sorted `(q, path, usr, vf)` and rollups by `rollup_sql` (heavy = more than R distinct roots under the directory: a run's sticky extra rollups disappear), with the per-file group indexes. This is the build's `build` stage minus the level loop.
- **Short literals.** `short-plan` cuts q-groups by each literal's root rows. A literal's rows through D are `G`'s (`drill/short-q.parquet`, written by this fold; the first fold reads `roots-measure/short/q/`) plus the runs' new identities. Per q-group, combine `G`'s rows (from `G`'s q-group holding the literal) with the runs', then `build_roots`, as `short-reduce` does.
- **`index`**: the build's `index` stage over `G'`'s per-file indexes, then `meta.json`.
- **Next fold and the daily runs' inputs**:
  - `drill/digest/s####.parquet` (per member, through D);
  - `drill/short-q.parquet`;
  - `roots-measure/dirs/s####.parquet` and `roots-measure/short/dirs/g###.parquet`: `dir_stats` (floor 10K) over the folded roots. Long rows are per member, an alias getting its canonical's rows. Short rows are whole q-groups, so there are no depth-1 partials, and `short/top/` is empty.

## Publishing and switching

- `G'` is written under `static-names/<G'>/` only. The job refuses if anything is there, writes nothing under `G`, and deletes nothing.
- The order is: the parts above; then `manifests/<last scan>.json` (`runs: []`, `scans` = all, `base_scans` = all); then `compacted.json` last. `compacted.json` holds `{from: G, runs, scans, plan, counts, md5 per served file}` and is the completion marker. The R2 copy (`r2-copy -g G'`, then `compacted.json`) follows the same order.
- **Switching** is one setting per side. Readers set `STATIC_GEN` = `G'` (a deployment var). The runner sets the profile's base generation to `G'`. Each switches only once `compacted.json` exists, on GCS for the runner and on R2 for readers. A scan after the cut (e.g. `2026-10-09T1236`) is appended to `G'` as its first run. Its `prepare` checks that it is the next scan after `G'`'s, so the runner cannot append to `G` and `G'` both.
- `G` and its runs stay, so a reader on `G` keeps working until it switches.

## Stages (Batch, `python -m dt_cloud.static_compact …`)

1. `plan` (1 task): `scans.json`, `ranges.json`, the runs, the target generation. Refuses a non-empty target.
2. `light` (per key range, then per shard): fold `cintervals` + `chist` per range; then `shards.json`; then `merge_shards` per shard of the plan (a task per shard group); `catalog`.
3. `drill-classes` (1 task): the folded digests → `drill/aliases.parquet`, `drill/digest/`; `members.parquet` (from `light`).
4. `drill-long` (queue over `G'`'s shards) and `drill-short` (per q-group): roots, rollups, per-file indexes, measurement.
5. `drill-index`, `manifest`, `compacted.json` (1 task).

## Acceptance (real data)

Compact `2026-10-08c` ⊕ the 2026-10-09 run into `2026-10-09c`, then compare a sample with a full rebuild through 2026-10-09:

- **Light.** `cintervals` for sampled ranges, rebuilt from the 71 scan files (`build_range(coalesced=True)`); `sx/` and `sidecar/` for sampled shards, rebuilt from those cintervals (the build's suffix map and shard writer for the shard's prefix range). Both md5-equal.
- **Drill.** For sampled shards and q-groups, the build's `build` / `short-reduce` from the rebuilt suffix shards / cintervals (the level loop, not the fold): md5-equal roots, rollups and per-file indexes, and equal aliases.

## Cost (estimate; measured figures go below once run)

- Light: ~5 GB of cintervals and 136 GB of shards, read and rewritten (≈ 2 VM-hours).
- Drill: 299 GB of roots, read and rewritten with the rollups recomputed (≈ 25–40 VM-hours, the build's `build` + `short-reduce` without the level loop).
- About $8–12 a month at spot prices. GCS storage for a second copy is ≈ $9 a month until `G` is deleted.
- R2: each compaction copies the whole new generation (≈ 435 GB × $0.12 ≈ $52 of GCS egress). That is the dominant cost and a reason to keep compactions rare, or to copy only what changed. Most drill and shard files change, though, because shard plans move.
