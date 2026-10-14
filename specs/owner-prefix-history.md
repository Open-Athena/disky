# Owner prefix history: one row per assigned prefix, not one per scan

Status: proposed (2026-10-14). `[cloud]`. Follows a92e0165 (no folded totals stored per scan) and ae99b621/13f56344 (a new prefix priced on every interval-store scan in one read).

## Problem

An owner-lens series folds the ledger over every scan. Folding needs each assigned prefix's bytes, objects and per-user/class breakdown on that scan. Today those live in `owner_prefix_aggs`, one ~260 KB row per (scan, index). So:

- a new assignment updates a row for **every** scan (one D1 batch, 1.2 s at 81 scans);
- a fold reads and parses a whole row per scan (≈21 MB at 81 scans);
- a new scan prices all ~1,260 prefixes again (13–22 s) though almost none changed.

All three grow with the number of scans; the goal is thousands. Yet the interval store already holds this data as versions, with each row live over `[vf, vt)` scans. A prefix whose total doesn't change across scans is one version.

## Design

`owner_prefix_hist (idx, depth, path, hz, versions, updated_ts, PRIMARY KEY (idx, depth, path))`:
- `idx` is the interval-store generation tag (as now);
- `versions` is `[[vf, vt, agg], …]`, the prefix's aggregate as it was over each scan range, in the store's own scan ordinals (`agg` = `PathAgg`: `b, o, us, uo, cb`);
- `hz` is the newest scan ordinal it is priced through.

Depth-1 roots (bucket totals) are rows too.

- **Assign a new prefix:** one history read (`readAsksHistory`, as now), versions built by `vf/vt` with no per-date expansion, **one row written**.
- **Owner change on priced prefixes:** nothing read, nothing written (as of a92e0165).
- **Fold at scan D:** for each wanted prefix and root, take the version with `vf ≤ asOf(D) < vt`. The rows load once per isolate per (idx, prefix set): size ∝ prefixes × versions, not × scans.
- **Series:** one pass over the loaded rows gives every scan's point. There's no per-scan D1 read at all.
- **New scan** (store horizon `> hz`): only versions open at `hz` can have changed. Price the stale rows over the new runs only and merge (close `vt`, append new versions), then advance `hz`. The scan job does this right after `interval-store append` (a `dt-cloud owner-prices extend`-style step, or a site endpoint the job calls), so no viewer pays for it.
- **Per-scan stores** (no interval store: cw today and older generations) keep `owner_prefix_aggs` as is.

### Needs from the index reader

- `readAsksHistory` returns the combined version rows (`vf`, `vt`), not only per-date live rows.
- It accepts a run filter: "runs beginning after `hz`, and close records ending versions open at `hz`", for horizon extension.
- A test oracle: for every held date, a fold from history rows equals a fold from per-date reads (the existing parity fixtures `g2`/`g5`, including close records and deletion/resurrection).

## Interval owners (discussion; not part of this change)

Today the current ledger applies to every scan. With time-bounded owners, an assignment of P to B at time t would apply from the previous assignment's handover on P (or from the start, usually) and forward until the next one. That's the same as today when P has no prior assignment.

It composes with this design: a fold at D takes the ledger state for D, so prefix rows stay as they are. It doesn't fix the scaling problem by itself. The scaling cost is in the prefix aggregates, not the ledger.

## Phases

1. Reader: versions out of `readAsksHistory`, plus parity tests.
2. `owner_prefix_hist` migration (gcs + cw lineages) and the fold from history. New prefixes write one row. Measure series after an assignment at 81 scans. Target: under 2 s after the history read.
3. Horizon extension in the scan job, and dropping `owner_prefix_aggs` writes for interval-store scans.
