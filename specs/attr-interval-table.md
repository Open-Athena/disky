# Attribution history as an interval-store table

**Status:** draft v1, 2026-10-23, for root's review before any implementation. Cross-deployment: the code is `[cloud]`; only gcs attributes today.

**Goal (Ryan, 2026-10-23):** stop writing a full `attr.tsv` (+ `attr.tsv.idx.json`) per scan. It maps every attributing prefix to `(user, source, evidence)`, and after `scan retire` it is the last per-scan copy whose cost grows with the number of scans ([lean-scans] §2 keeps it, `scan_retire.py` class `extras`). Its history goes into the [interval store] as one more versioned table, sharing the store's key ranges, generations, appends, carries and compaction.

**Out of scope:** user assignments (the D1 actions ledger, applied at read time by `ownerAssignments`) stay in D1. `ck.txt` (the other `extras` file) is untouched. The static name index and its compaction are untouched.

## 1. Today

- `path-index -a` (and `path-index-merge`, `index_parts.py:515`) calls `prefix_labels` (`viz.py:33`) on the scan's attribution parquets (`job/run.sh:292`: `attribution-2026-07-20.parquet`, `attribution-wandb.parquet`, which is rewritten in place) and the scan's listing dirs (path-glob rules expand against them), then `write_extras` (`extras.py`) writes `attr.tsv`: one line `key \t user \t source \t evidence` per prefix, sorted by key, `''` for a missing source or evidence, plus a 64 KB block index.
- The inputs are mutable, so the map can't be recomputed for an old scan. That is why retire keeps it.
- The site (`_lib/extras.ts`) finds the scan's generation dir through the D1 `path` pointer (`schemaRow(..., 'path')`), range-reads the blocks covering `[P, P0)` plus one block per ancestor of P, and answers `provenance(path, user)`: the deepest attributing ancestor-or-self whose user is `user`. `view.ts:2136` reads it beside every view; `subtree.ts:153` keys the response cache on `hasExtras`.
- **A live gap:** `scan retire -P` deletes the `path` pointer. `extras.ts` then falls back to `listing/<date>` and finds no sidecar, so provenance disappears for those scans even though `attr.tsv` is kept. This table removes the pointer dependency.

Churn: a prefix's attribution changes when a roster input changes or a path-glob rule matches a new dir. Most prefixes keep the same `(user, source, evidence)` scan after scan, so the map has the same shape as the store's other tables: few versions per key.

## 2. The table

### 2.1 Rows and columns

One row per attribution version, per attributing prefix:

| Column | Type | Role |
|---|---|---|
| `depth` | INT (as `PVL_SCHEMA`'s) | key: the path store's depth of `path` (`prefix_labels`' `depth`, `count('/') + 1`: a bucket is 1) |
| `path` | VARCHAR | key: the attributing prefix, an index path (`<bucket>[/<dir>…]`, no scheme, no trailing slash) |
| `vf`, `vt` | BIGINT | the version's `[vf, vt)` in scan epochs, `OPEN` while open: the store's own interval columns, kept as named because the shared kernel (`append_intervals`, `seg_sql`, the group bounds, `combineLive`) reads them by those names |
| `user` | VARCHAR | **change**: the canonical user the prefix is attributed to |
| `source` | VARCHAR, nullable | **change**: which rule or input attributed it (`manual`, `wandb-run`, …); NULL where `attr.tsv` has `''` |
| `evidence` | VARCHAR, nullable | **change**: the supporting detail; NULL where `attr.tsv` has `''` |

- Identity is `(depth, path, vf)`, as `pvl`'s (`interval_append.IDENT`). The map is one user per prefix, so there are no slices.
- Deltas add `op` (1 opened, −1 close record), as every other table's.
- Descriptive names throughout: the range dir and the served sort are both `attribution`, not a short code.
- `user`, `source` and `evidence` are always quoted in SQL (`q()`), since `user` is a keyword in some dialects.

### 2.2 Files

| Where | File | Written by |
|---|---|---|
| base | `attribution/r####.parquet`, sorted `(depth, path, vf)`, per key range of `ranges.json` | `build` (one machine and staged `build-ranges`), compaction, the backfill (§4) |
| base | `served/attribution.parquet` + `.groups.parquet` | `cut` / `cut_gen` |
| run | `deltas/<D>/attribution/r####.parquet` | the append's ranges stage |
| run | `deltas/<D>/served/attribution.parquet` + `.groups.parquet` | the append's publish stage, carries |
| scratch | `state/<D>/attribution/` | the staged append's open state (one machine: refolded, as `open_state_sql` does) |

The key is the same `(depth, path)` as `pvl`'s, so an attributing prefix lands in the key range that holds its directory. The store's 256 ranges, generations, manifests, carries (`merge_range`: identity, smallest `vt`, largest `op`), compaction (`compact_range`), prune and R2 copy then handle it as one more table, with no new partitioning.

### 2.3 The served sort: `attribution`, ordered `(path, vf)`

- **Path first, not depth first.** The only read is "every prefix under P, plus P's ancestors" (`extrasFor`). Under `(path, vf)`, the keys under P are one contiguous span in each segment (`[P, P + '0')`, `'0'` sorting just past `/`), and an ancestor is a point read. A `(depth, path)` order would scatter a subtree across every depth.
- Dyadic segments as every sort (`seg_sql`), 8K-row groups, zstd. Group bounds as the other sorts': `p_min`/`p_max` prune the span, `vf`/`vt` prune by time; no size column (`SORTS` entry `("attribution", "path, vf", None)`, as `reads`).
- It is not a path-store tier: no view, diff, lens or filter reads it. Only `extras.ts` does.

### 2.4 Coverage: which scans the table answers for

A scan's map exists only if its `path-index` ran with `-a`. A scan without one (a failed attribution input, a deployment that doesn't attribute) must read as "no attribution known", never as "every prefix unattributed". So:
- **An uncovered scan contributes no input.** Versions are computed over the covered scans only; the open state carries over an uncovered scan unchanged. A version live across an uncovered scan does not mean that scan had it.
- **The covered scans are recorded:** the base's `scans.json` carries `"attribution": [scan ids]`; each run's `deltas/<D>/scans.json` carries `"attribution": true | false`; each manifest carries `"attribution": [the scan ids of its runs that carry the table]`, so a reader knows coverage from the state it already reads (`scans.json` + the newest manifest, `ivStateDoc`). A carry's merged run unions its inputs'; compaction writes the union into the new base.
- **A generation without the key has no table** (every generation today; cw-s3, r2.rbw.sh, hf, m3 for good). Its sort set excludes `attribution` (`served_sorts`), as an unsliced generation's excludes the slice sorts.
- **A table may begin at a run.** A base without `attribution/` range files is an empty open state, so the first appended scan with a map opens every prefix. Turning attribution on (or adding it to a deployment that starts attributing) needs no new generation. Its history before that point needs one (§4).

### 2.5 Exactness

For every covered scan D: the rows live at D (base + runs, combined per identity with the smallest `vt`), rendered as `attr.tsv` lines (`path \t user \t source or '' \t evidence or ''`, sorted by `path`), equal that scan's `attr.tsv` byte for byte. That is the table's reconstruction check, run:
- by `build` per range and scan (count + Σ hash per `(range, scan)` in the range digest, as `pv`'s);
- by the append's ranges stage (the new open state is D's map in the range exactly, count and Σ hash, or the range raises);
- by `interval-store verify`, as a new check over every sampled scan (and every scan in retire's verify marker, §5): whole-file byte equality against the scan's `attr.tsv`.

## 3. Writes

### 3.1 Input

The append's input for scan D is D's `attr.tsv`, found beside D's `path` sort through the profile's layout templates (`append.layouts` with `path-index.parquet` replaced by `attr.tsv`; the same `{gen}` the path sort resolved to). `prepare` pins it in `deltas/<D>/scans.json` beside the path sort (GCS generation, size, crc32c); a missing `attr.tsv` pins `"attribution": false`. The ranges tasks read it with DuckDB (`read_csv`, tab-separated, all VARCHAR, `''` → NULL) filtered by the range's `range_preds`. No new per-scan file is written.

### 3.2 The profile flag

`append.attribution: true` in the interval profile (gcs: `job/interval-store.json`); default off, no deployment default in `[cloud]` code. `build` and `backfill-attribution` take `-A TEMPLATE` (the `attr.tsv` layout, `{id}` / `{gen}`). With the flag off, nothing below runs and no file is written.

With it on:
- **Not refused:** a scan with no `attr.tsv`. It is recorded uncovered (§2.4) and logged as a warning the scan step surfaces.
- **Refused** (exact messages, tested): an `attr.tsv` that isn't sorted by key or repeats a key.

### 3.3 Stages

| Stage | Change |
|---|---|
| `build` / `build-ranges` | `build_attribution_range` per range: versions over the covered scans (the kernel's gaps-and-islands, change columns `user`, `source`, `evidence`), digest per (range, scan) |
| `cut`, `cut_gen` | `attribution` among the sorts when the generation has the table |
| append ranges (staged and `interval_local.add`) | `append_intervals` for `attribution` beside `pvl`/`sv`/`svt`, its delta and open state |
| publish, `cut_run` | the run's `served/attribution.parquet` + groups; `meta.json` and the manifest record coverage |
| carries (`interval_append.carry`, `merge_range`) | `attribution` merged as the others; coverage unioned |
| compaction (`compact_range`, `fold_gen`) | base range file + every run's deltas, one row per identity with the smallest `vt`; then cut |
| `r2` / `r2_publish` / `prune` | the new files ride the existing listings (a run's `served/`, its range dirs); no new rule |

**Byte identity, tested (TFFP where a fold is involved):** base + runs (merged or not) equals a fresh `build` over the same scans, range file for range file and served sort for served sort; a compacted generation equals a fresh build; a backfilled generation equals a fresh build; a carry's merged run equals the pairwise one. Fixtures: the existing interval fixtures plus per-scan `attr.tsv` files with a user change, a source change, an evidence change, a prefix added, a prefix removed, a prefix that comes back, and an uncovered scan in the middle. Unattributed fixtures (`g7`, the unowned scans): no `attribution` file anywhere, no `"attribution"` key, every existing byte unchanged.

## 4. Migration: history from the kept `attr.tsv` files

**`dt-cloud interval-store backfill-attribution -g OLD -O NEW -o STORE -s ROOT -A TEMPLATE`** (`interval_backfill.py`'s pattern, beside `backfill-generation`):
1. Fold OLD's base and its newest manifest's runs into NEW, as compaction does (`interval_compact.fold_gen`), so NEW has no runs.
2. Read each of NEW's scans' `attr.tsv` (TEMPLATE under ROOT; a scan's `{gen}` from its pinned `deltas/<id>/scans.json` or the D1-free layout search `append.layouts` already does). A scan without one is uncovered.
3. Build `attribution/` per range over the covered scans (`build_attribution_range`), cut the sort, write `"attribution": [ids]` into NEW's `scans.json` and `backfill.json` (`from`, the runs folded, the scans covered and not).
4. Where OLD's runs already carried the table (attribution was turned on before the backfill), their combined rows must equal what step 3 built for those scans; otherwise it refuses.
5. Publish as `backfill-generation` does: NEW's files, `scans.json` last; `CURRENT.json` untouched. Verify NEW (`interval-store verify`, including §2.5's check over **every** covered scan, not the sample), then `interval-store current -s NEW` and bump the profile's `append.gen` / `ranges_gen`.

It reads one small file per scan and writes no per-scan copy. It runs on one machine (mgu or a Batch task), never the laptop.

## 5. Reads (`site/functions/_lib/extras.ts`)

**Attribution at scan D, for a view rooted at P:**
1. Resolve the served interval generation as views do (`ivGen`: `INTERVAL_STORE_GEN` pin, else `CURRENT.json`) and its state (`scans.json` + newest manifest, already cached a minute per isolate and in the colo cache).
2. If D is in the state's attribution coverage: open the `attribution` sort over the base and the runs begun at or before D (`asOfScans`), plan groups with `p_min < P0 ∧ p_max ≥ P` plus the groups holding each ancestor of P, and `vf_min ≤ D < vt_max`; decode, combine per identity across tiers (`combineLive`, smallest `vt`), keep `vf ≤ D < vt`. `viewOf` runs unchanged over those rows.
3. Else: the per-scan sidecar, as today (a scan before the table, a deployment without it, an uncovered scan whose `attr.tsv` survives).
4. A cap as today's `MAX_RANGE`: when the planned groups hold more than `ATTR_MAX_ROWS` rows (a bucket root over a huge map), extras are skipped for that view (null, today's behaviour), never a partial map.

- `hasExtras(env, date)` is true when the generation covers D. The subtree response cache already keys on the generation (`pathGens`), so a backfilled generation re-keys every cached answer.
- No D1 read: the `path` pointer dependency (and the `retire -P` gap, §1) goes away for covered scans.
- **Vitest** (`extras.test.ts`, `intervalStore` fixtures built by `gen.py` with the table): for every fixture scan and every directory P, and each user, `extrasFor(...).provenance` from the table equals the per-scan sidecar's; an uncovered scan falls back to its sidecar (and null without one); a run's change is visible only at and after its scan; an as-of read leaves later runs out; the cap returns null; an unattributed generation never opens the sort.

## 6. Deployments

| Deployment | Attributes? | Effect |
|---|---|---|
| gcs | yes (`path-index -a`) | the table, after the backfill (§4) and the flag |
| cw (cw.oa.dev) | no `-a`, no owner axis | none: no flag, no files, no `"attribution"` key; `extras.ts` keeps returning null |
| r2.rbw.sh (the r2 demo) | no | none (one-machine `add`: the flag is off, so nothing runs) |
| hf, m3 | no | none |

A deployment that starts attributing turns the flag on; its next appended scan opens the table (§2.4), and its earlier history, having no `attr.tsv`, stays uncovered. Parity-by-default: any deployment with attribution gets the table; the others have nothing to skip.

## 7. Rollout (gcs)

1. Land `[cloud]` code with the flag off (no generation changes, no deployment behaviour change). The site reads the table only where a generation has it.
2. Backfill: `backfill-attribution -g <current> -O <next>` on mgu or Batch; verify over every covered scan; deploy the site (dev first: `site/deploy --dev`, then prod); `interval-store current -s <next>`; bump `job/interval-store.json` `append.gen` / `ranges_gen` and set `append.attribution: true`; rebuild the job image before the 07:00 daily.
   - Alternative order: flag on first (the next scan opens the table in the current generation, no new generation), backfill later (step 4 of §4 checks the runs against it). Either works; backfill-first gives full history from day one.
3. From then each scan's append writes the table's delta; `attr.tsv` is still written per scan (the append's input).
4. **Retire** (`scan_retire.py`): split the `extras` class. `attr.tsv`, `attr.tsv.gz` and `attr.tsv.idx.json` become class `attribution`, deletable for a scan once (a) the serving generation (R2's manifest, as the existing gate) covers it and (b) its verify marker includes §2.5's check passing; `ck.txt` stays `extras`, kept. The existing `KEEP` window (latest 7) and grace apply. `publish-r2` stops requiring and copying `attr.tsv` once the site reads the table (the scan step's `require` list, `test_scan_steps.py`).
5. End state: `attr.tsv` exists only for the newest `KEEP` scans (the append's input and its fallback window). Per scan, the store adds only the map's changes.

## 8. Open questions for review

1. **`vf`/`vt` names.** Kept as the store's interval columns (shared kernel and reader). Renaming them store-wide (`valid_from`/`valid_to`) is a separate, larger change across every table and generation.
2. **Input file.** §3.1 reads `attr.tsv` as is. Alternative: `path-index -a` writes an `attribution.parquet` (descriptive columns, typed NULLs) as the append's input instead, and stops writing `attr.tsv` + index once the site reads the table. Cleaner end state; one more format change in `path-index` and `path-index-merge`.
3. **Coverage in manifests** (§2.4) vs. a per-run `meta.json` field the reader would have to fetch per run. The manifest field costs nothing at read time.
4. **Rollout order** (§7 step 2): backfill-first or flag-first.

[lean-scans]: lean-scans.md
[interval store]: interval-store.md
