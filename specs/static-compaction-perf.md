# Static compaction and carry merges: why they take 12+ h and 361 GiB, and the levers

Status: analysis (2026-10-23) plus two levers built in `cc153391` (see "Built"). Remote tests have **not** run yet (the test node needed an AWS SSO login). Target: a compaction under 1 h on one VM, and a working set that doesn't scale with the corpus (≤ 64–128 GiB RSS).

## The run being explained

gcs's compaction of generation 2026-10-08c (base + 4 runs, ~97 scans) on n2-highmem-48 (361 GiB container), DuckDB `-M 270GB -p 48`, image before `ab97c805`:

| step | s | RSS peak GiB | disk peak GiB | rows |
|---|---:|---:|---:|---|
| intervals | 1,328 | 24 | | |
| shards | 1,250 | | 160 | |
| catalog | 1,341 | 75 | | |
| drill/fold/plan | 3,590 | 174 | | 47,595 tuples, 4,858 chunks |
| drill/fold/runs-spill | 2,068 | 272 | 210 | 4 runs |
| drill/fold/long-roots | 12,246 | 356 | 681 | 40.06 B |
| drill/fold/canonical-roots | 5,584 | | 1,085 | 39.51 B (45,109 canonical) |
| drill/fold/short-roots | 8,912 | 361 (= limit) | 807 | 15.27 B |
| measure, write, anchors, upload | still running at 12 h+ | | | |

The 10-22 L3 carry (`merge_tiers`, 2.03 B rows, 19.6 GB in): read 60 min long / 17 short, fold 14 / 5.5, write 51 / 18.

## Root cause 1: the rows are the drill, not a blow-up; the fold makes ~6 passes and a global sort over them

The "~95 B intermediate rows from a few B" reading is not what happens. The drill **is** ~55 B rows: one root row per (heavy literal `q`, root path, owner slice, version), and each changed path is indexed under many literals. 45,109 canonical long literals × ~0.88 M roots each ≈ 39.5 B, plus 15.3 B short-literal roots; the base's `drill/` (279 GiB) holds the same order of rows. The three counts are three passes over the same data:

- `long-roots` (`static_compact.py:195-215`) combines each member-tuple's rows across tiers (base read per chunk by its classes' `q` span, the runs' rows from the spill) and digests them: 40.06 B rows (tuples, `tid`; 1.4% more than the canonical set, because 47,595 tuples merge back to 45,109 classes by digest).
- `canonical-roots` (`static_compact.py:231-239`) reads all of them back, renames `tid` → canonical `q`, adds `depth`, and re-partitions from tuple order to `q`-range order (`spill_roots`): 39.5 B rows written a second time. It exists only because the output must be in `q` order and the chunks are in tuple (`c0`) order.
- `short-roots` (`static_compact.py:253-267`): the short literals' 15.3 B rows, combined and spilled by `q` range.

Then `finish_drill` (`static_local.py`, before `cc153391`): `measure_ranges` reads every range back for the measurement and rollups (pass 4), and `build_drill` → `write_kind` with no `chunks` ran `_cut` (`GROUP BY q` over a view of every spill, pass 5) and, per output file, `SELECT … FROM <view of every spill> WHERE q range ORDER BY q, path, usr, vf` (pass 6, a DuckDB external sort of 55 B rows with ~1 TB of temp). That sort is the 12 h+ tail. `ab97c805` streamed `merge_tiers`' write but not this one.

The steps also pay twice for `q`-range assignment: `spill_roots`, `merge_tiers`' read and `short-map` used `rows ASOF JOIN bounds ON q >= lo`. DuckDB's ASOF join has no equality key here, so it sorts its whole left side in one partition (`asof_loop_join_threshold` = 64 only covers tiny left sides). Every spill was a sort of its input. This is the likely main cost of the carry's 60 + 17 min `read/*`: 2 B rows at ~0.5 M rows/s, far below parquet decode or gcsfuse throughput. It is also paid per chunk in `canonical-roots` and `short-roots`.

`drill/fold/plan` (`static_compact.py:172-180`) is a full pass too, 1 h. `tc{i}` counts every tier's long roots per `q` (the base's 40 B rows from the GCS mount) only to size chunks. An estimate would do, because chunking never changes bytes.

## Root cause 2: RSS sits at the container limit by design, and above `-M` from allocations DuckDB doesn't track

- `-M 270GB` lets DuckDB's buffer pool grow to 270 GB, and it does. DuckDB 1.5's `enable_external_file_cache` (default on) keeps the parquet bytes it reads in that pool, and materialized intermediates (`tc*`, `mt`, `tup`, `ct`, the `lrup`/`srup` rollup tables) sit in it until evicted. The plan step's 174 GiB is a read-only scan filling the cache. RSS ≈ the limit says nothing about need. Every fold step is already chunk-bounded (~8 M rows a chunk), so the need is a few GiB per thread.
- RSS went ~90 GiB past the 270 GB cap. `rss_peak_gb` is `ru_maxrss`, a monotone peak, so the jump is from `runs-spill` (174 → 272) and `long-roots` (→ 356). `runs-spill` is a `COPY … PARTITION_BY (ch)` over **4,858** partitions. DuckDB keeps per-thread, per-partition buffers (`partitioned_write_flush_threshold` 524,288 rows) and closes and reopens files past `partitioned_write_max_open_files` (100), and that memory is not all under `memory_limit`. Add jemalloc retention and the Arrow results pulled into Python. Pyarrow itself is not the term: the only large Arrow objects are `aliases` (94 K rows) and the writers' row groups.
- Fix: size `-M` to the working set (e.g. `-M 48GB` on this VM) and partition spills into tens or hundreds of files, not thousands. The `PARTITION_BY` fan-out is lever 4. With the global sort gone (`cc153391`) nothing in the fold needs more than a chunk plus the rollup tables.

## Floor

Input: the base drill, 279 GiB (~55 B rows), plus the runs' drills (tens of GiB); output about the same. The intrinsic work is one decode of the inputs, a per-`q` combine (min `vt` per key: a merge of sorted inputs, not a hash aggregate), a per-`q` measurement and rollup, and one encode plus index. On 48 cores, figure ~1 GB/s of compressed parquet per pass for DuckDB decode, plus parallel zstd encode:

- read + combine: ~5–10 min
- measure + rollup (per-`q`, chunked, already parallel in DuckDB): ~10–20 min
- write: 55 B rows. The serial writer (`write_sorted`, one Python callback per 8,192-row group, ~6.7 M groups) is ~1.5–3 h single-threaded, ~10 min over 16 processes.

Drill floor ≈ 30–45 min. The non-drill parts (intervals, shards, catalog: 65 min today) run one range at a time (`fold`'s `jobs` is unused) and parallelize the same way, to ~10 min. **< 1 h end to end is reachable, but not with the current pass structure.**

## Levers, ranked

1. **Stream the base drill's write.** Built in `cc153391`. `measure_ranges` writes each `q` range's roots sorted (it already holds them in a table for the measurement) and deletes its spill; `build_drill(sorted_roots=…)` hands those files to `write_kind`'s `chunks`, as `ab97c805` did for `merge_tiers`. This removes pass 5 and pass 6 (the 55 B-row external sort and its ~1 TB temp): an estimated several hours (the L3 carry spent 69 of 185 min in the same sort at 2 B rows). Work disk peaks at the spills *or* the sorted ranges (~0.5 TB), not both plus the sort temp. It also covers the one-machine `static-names build` (shared `finish_drill`).
2. **Assign `q` ranges by a hash join, not a per-row ASOF.** Built in `cc153391`. `static_roots.ranged`: the ASOF join runs over the distinct `q`s only (≤ ~100 K; `merge_tiers` uses `cmap`'s merged canonicals), and rows take their range by an equi-join. Same rows, same ranges. Estimate: the carry's `read/*` from 77 min to I/O-bound (~5–10 min for 19.6 GB). Smaller per-chunk wins in `canonical-roots`, `short-roots` and `short-map`.
3. **Write files in parallel.** Not built. `write_kind`'s cuts are known before any file is written (`_cut_counts`), and each file and its index rows depend only on its own range. Run them over a process pool (each process opens its chunk files by range), then concatenate the index tables in file order. The bytes are unchanged by construction. Estimate: the write from hours (serial, ~5–10 M rows/s) to ~10 min. After 1 and 2, this is likely the largest remaining term, both here and in `merge_tiers` (the carry's 51 + 18 min write).
4. **One pass for the long roots, no `canonical-roots`.** Not built. Order tuples by their least member, not by the base's class. The kept tuple of each merged class is the one holding the class's least member, and its canonical `q` is that tuple's least member, so chunks of consecutive tuples produce `q`-disjoint, in-order output. `long-roots` can then write each chunk's kept tuples renamed and sorted, measure them in the same table, and skip the second pass and the spill (−1.5 h, −0.5 TB). The catch: a chunk's base classes then no longer span a narrow `q` range, so base reads by `c0` span get wider. Spill the base once by chunk, as the runs are (one sequential read of 279 GiB, instead of `q_spans` reads per chunk), and use few partitions (lever 5). Tuples merging back by digest (47,595 → 45,109) still need the digests before any write: keep the per-chunk `ct` files and choose the kept `tid`s after digesting, as now. Only the rename and sort move into a chunk-local second step that reads `ct`, not a global re-partition.
5. **Fewer partitions in spills; plan from the index.** Built (see "Built (lever 5)"). `runs-spill` partitions by chunk (4,858). Partition by ~64–256 super-chunks instead and filter by `ch` on read (row-group pruning on the sorted `ch`). This bounds open files and the untracked partition buffers that pushed RSS past `-M`. The plan's `tc{i}` counts can come from each tier's `long-roots-index.parquet` (per 8,192-row group: `q` first and last, rows). Approximate counts are enough because chunk sizes never change bytes. −1 h of plan and the 174 GiB cache fill. The same applies to `merge_tiers`' `plan` count pass.
6. **Size `-M` to the working set and turn off the external file cache for these jobs.** A config change: `-M 48GB` (or a fraction of RAM, chunk-bounded), `SET enable_external_file_cache=false` in the build's connections. RSS then tracks the chunk, not the limit. Do this once 1–5 have removed the unchunked steps; before `cc153391`, the global sort needed the large limit to avoid spilling 1 TB.
7. **Parallelize the light parts.** Not built. `fold_range` per key range and `fold_catalog`'s census per shard over `sn.each` (`jobs`): 65 → ~10–15 min.

Estimated end state (1–7): read and combine ~15 min, measure and rollup ~15 min, write ~10 min, light parts ~15 min, upload apart. About 1 h on n2-highmem-48 with `-M` ≤ 64 GB. With 1–2 alone, the 12 h+ run should fall to roughly the steps before measure (~9 h, unchanged) plus measure and a serial streamed write (~2–3 h), so 3 and 4 are needed to reach the target.

## The carry merge (`merge_tiers`)

The carry shares levers 2 (its `read/*`), 3 (its write) and 5 (its `plan` count pass). Its fold (14 + 5.5 min) is already chunked. Its write already streams (`ab97c805`). After 2 and 3, the L3 carry's ~185 min should be ~30 min, dominated by decode and encode.

## Built (`cc153391`)

- `static_roots.ranged(rows, bounds, col, keys=None)`; used by `static_local.spill_roots`, `static_drill.merge_tiers` (keys: `cmap`'s distinct `c`) and `static_roots` `short-map`.
- `static_local.measure_ranges` returns each range's sorted roots file (`<kind>-sorted/g<g>.parquet`, spills removed per range); `finish_drill` passes them to `static_drill.build_drill(sorted_roots=…)`, which hands them to `write_kind(chunks=…)`.
- Byte-identity is pinned by the existing comparisons against the unchunked writer: `test_build_drill_is_the_reference_drill` (the reference is `build_drill` over tables, sorted by DuckDB), `test_build_chunked_drill_is_the_unchunked_one` (`-c 1`: one range per literal, so many sorted files with output cuts across them), the compaction tests (fold = rebuild), `test_chunked_drill_merges_write_the_same_bytes` (`merge_tiers` at several `chunk_rows`), and `test_writing_sorted_chunks_writes_the_same_bytes`. New: `test_ranged_assigns_the_asof_ranges` (equal to a per-row ASOF join, at bound counts below and above DuckDB's loop-join threshold).
- Not yet verified: `scripts/remote-test cloud/tests/test_static_roots.py cloud/tests/test_static_drill.py cloud/tests/test_static_local.py cloud/tests/test_static_compact.py cloud/tests/test_static_merge.py` (blocked on `aws sso login --profile oa-ci-dev` to start the node). The ASOF-sort diagnosis is from DuckDB's ASOF design and the carry's throughput; it has not been profiled on real data. A `-c` probe (`static_merge probe`) on the 10-22 carry before and after `cc153391` would settle it.

## Built (lever 5)

- `static_roots.index_estimates(con, index_files, keys, into)`: per key, its rows estimated from `GROUP_INDEX_SCHEMA` indexes alone (each group's rows spread evenly over the keys in its `[q_min, q_max]`, the remainder to the first; exact for single-`q` groups). The compaction's `drill/fold/plan` (`tc{i}`: per tier, keys = its alias map's canonicals), its short-roots plan (keys = the short indexes' group bounds; the per-range file selection now uses the range bounds, not the counted literals) and `merge_tiers`' `merge/<kind>/plan` (per input, keys = `cmap`'s `ci`, summed per merged `c`) read the indexes instead of counting every row. `merge_tiers` also skips the `lit` scan for the long kind (only the short kind's `cmap` uses it).
- `static_roots.ChunkSpill`: a spill by chunk `ch` goes into at most `SPILL_PARTS` (64, under DuckDB's 100 open files) partitions of consecutive chunks; the reader, reaching a partition of several chunks, splits it by chunk (one more local pass over that partition, fan-out ≤ ⌈chunks / 64⌉) and drops it on moving on. Used by the compaction's `runs-spill` (4,858 partitions → 64) and `merge_tiers`' `read/*` (unchanged while a carry has ≤ 64 ranges). `fold_drill`'s chunk numbers are made dense first, so partitions are even.
- Byte identity: chunk sizes and partitions never change output. Pinned by `test_compacted_generation_is_the_rebuild` (new worlds `d`, `e`: `-c 3` with 2 and 1 partitions), `test_chunked_drill_merges_write_the_same_bytes` (new: `chunk_rows=2` at 1 and 2 partitions), `test_index_estimates_spread_each_group_over_its_keys`, `test_chunk_spill_reads_each_chunk_exactly` (1, 2, 3, 7, 64 partitions over 7 chunks).
