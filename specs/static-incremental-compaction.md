# Static compaction: incremental, not O(corpus)

From gcs's design exploration (`wt/gcs/tmp/static-incremental-measure.md`, 2026-10-24), reviewed against the code at `cloud`.

## Where the time goes (gcs `2026-10-08c → c001`)

- **Fold:** long-roots 3,054 s, canonical-roots 2,054 s, short-roots 1,471 s.
- **Measure:** ~6 h.
  - The sampled `measure-probe` shows the measure stage's wall clock is set by a few unsplittable single-`q` ranges of 100M–440M roots, which spill to temp. Everything else is cheap.
  - Rollups outweigh `dir_stats` about 2.5:1.
  - ef95df4d measures those heavy ranges first, on wide processes. Re-measure after it before sizing the rest.
- **Rest:** intervals, shards, catalog and anchors stay O(corpus) either way.

## Review of the claims

- **Overcounting: right.** A run's `dcount` and its newly-heavy sum `S` count every delta root, closes included (`tdir.rows`). The run's own header counts only opens (`h.hb + t.opens`). The overcount is conservative: a run may roll up a directory a rebuild wouldn't, never the reverse, and answers stay exact. That's why compaction is the reset.
- **Additivity: right**, with these caveats:
  - Root counts per `(q, dir)` are additive in new keys. The heavy / `floor` sets only grow.
  - Child event series append after every base event (every run event is at `t ≥` the run's scan).
  - Peaks are monotone.
  - The kept top-K changes only by a restatement, which runs already do. A new member's history is all new.
- **Byte identity for design 1 is not shown yet.** "Base cells ⊕ run cells by the newest full header, collapsed into the rebuild's form = the rebuild's cells" is the claim a test has to establish. A carry's merged drill isn't tested against a rebuild today, only answers are.
- **Design 2 trade-off.** It keeps the base drill forever and adds a tier. It saves the drill rewrite and its R2 copy, at the cost of reader fan-out. It's worth it only once design 1's numbers are in.

## Plan

1. **Exact `S` and `dcount` (done in this change).** A run counts the roots new to the stack: an existing member's opens, every root of a new member (`tdir.fresh`). Old runs keep their overcount, which is conservative. This yields fewer spurious newly-heavy directories, fewer base counts and probes, and is the enabling change for both designs.
2. **Additivity test (next).** `incremental_measure(base, runs)`: the base's `roots-measure` ⊕ the runs' new-key counts (+ the floor crossers' base counts) must equal the rebuild's `roots-measure`, md5 for md5, over the `test_static_compact` worlds.
3. **Classes from digests.** Persist per-member delta digests in runs (close: `+h(new) − h(old)` in HUGEINT) and the base's digests (a one-time pass), so a compaction's classes come before any root is read. That removes the fold's digest pass.
4. **Design 1 measure and rollups.** Replace `measure_ranges` in a compaction with base ⊕ runs. Byte identity is checked by `test_compacted_generation_is_the_rebuild`, unchanged.
5. **Decide design 2** from the numbers after 4 and the staging levers.
