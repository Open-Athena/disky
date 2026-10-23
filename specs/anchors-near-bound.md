# Anchors: a tight bound for "near" `(k, dir)`

## Problem

A run's anchors (`static_anchors.run_rollups`, `start_run`) find the `(k, dir)` that become heavy with the run (prior rows + the run's > `R` = 100,000). Exact prior rows are expensive (`probe_prior_rows` reads every tier's rows of each key), so a cheap bound picks the candidates first (`near_candidates`), and only those are probed.

On cw's backlogged 2026-10-20T1806 scan (n2-highmem-16, 7,142 s, cgroup peak 123 GiB) the bound let almost everything through:

| mode | near `(k, dir)` | keys probed | prior rows read | newly heavy | probe time |
|---|---|---|---|---|---|
| `end` | 37,324,100 | 3,889,815 | 401,432,349 | 7 | 50 min + 17 min `pd` |
| `exact` | 2,635,173 | 211,005 | 11,590,500 | 1 | 3.5 min + 2 min |
| `start` | (no bound) | 1,014 keys, 1,340 batches | 207,870,298 | 0 | 41 min |

About 620M prior rows were read to find 8 newly heavy `(k, dir)`.

## Why the bound is loose (hypothesis, to confirm in step 1)

`near_candidates` bounds a `(k, dir)`'s prior rows by the rows of every tier's row group whose `(s, path)` statistics meet `[(k, dir/), (k, dir0))`. The bound is good for a group holding only `k` (`s_min = s_max = k`): its `p_min`/`p_max` are `k`'s own paths. It is useless for a **mixed** group, which holds other keys: its path statistics span the other keys' paths, so it meets every `dir` and counts all its rows (up to `SX_RG` = 8,192) toward every `(k, dir)`.

- In the base, a key has at most two mixed groups (its first and last), so the error is about 16K rows.
- In a run tier, a key's rows are usually few and sit inside one mixed group, so each run tier adds up to 8,192 rows to every `(k, dir)` of the key. With 13+ run tiers that alone passes `R`.

So a key touched by the scan with a few tens of thousands of rows overall gets every one of its touched dirs marked near.

`start` has no bound at all: every touched prefix not already heavy is probed (`near = rd ANTI JOIN ps`).

## Plan

1. **Measure (read-only).** Add `dt-cloud static-names anchors near-probe -g GEN -d SCAN -m MOUNT -o OUT`: run steps 1–3 of `run_rollups` for one mode over a published generation's tiers and the scan's run, writing nothing to the store, and report:
   - `near` per bound variant;
   - the exact `pd` counts;
   - the newly heavy;
   - each tier's share of the bound, split into pure and mixed groups.

   Run it on cw's 10-20T1806 and on gcs's latest scan, to confirm that run tiers' mixed groups are the slack.
2. **Exact run-tier counts.** Run tiers are small next to the base. Count their rows per `(k, dir)` exactly for the keys in `ck`, reading `s` and `path` with `s` pushdown. Bound only the base by groups, so near is `n + base_bound + runs_exact > R`. The probe then reads only the near keys' rows, as today.
   - If (1) shows merged runs (L2+) are too big to count this way, give each tier a per-key census instead: `(s, rows)` for keys with ≥ `F` rows, written with the tier.
     - A key absent from the census bounds at `F − 1` per tier.
     - A run's census is exact, and a merged run's is the sum of its inputs'.
     - A compaction recounts it.
3. **`start`.** Gate prefixes the same way: a prefix's prior rows bounded by the tiers' groups over the name index's `/prefix` range, base by groups and runs exact. Probe only those that can pass `R`.
4. **Tests.**
   - Near under the new bound ⊇ the exact newly heavy, on fixtures with mixed groups in run tiers. Assert the near set exactly.
   - The anchors output is byte-identical to today's, since only what is probed changes, not what is written.

## Ownership

Root session on `cloud`. cw holds its `near_candidates` changes until this lands.
