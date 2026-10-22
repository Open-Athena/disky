# D1 footer writes: who still reads `index_row_groups` on gcs

**Status:** 2026-10-21. Option (a) built and turned on in gcs's profile (`options.index-sync.d1_rows: false`); not deployed (the job image needs a rebuild). No D1 row was written or deleted for this spec: every number below is a read-only query of `oa-gcs-usage-auth`.

## 1. The cost

Each full gcs scan's `index-sync` writes one `index_row_groups` row per row group of each per-scan sort, three sorts (`path`, `bysize`, `bysize-user`, `-u bysize`):

| scan | `path` | `bysize` | `bysize-user` | rows |
|---|---|---|---|---|
| `2026-10-21T0701` | 82,060 | 82,060 | 82,060 | 246,180 |
| `2026-10-21T1439` | 82,520 | 82,520 | 82,520 | 247,560 |

(`SELECT date, variant, COUNT(*) FROM index_row_groups GROUP BY date, variant`, 2026-10-21.) Those two scans are the only primary rows in D1: `retire` (`keep: 2`) deletes every older scan's rows. Each row is ~750 B of text (`footer_cache.bytes / rows`), and touches the table, its PK index and two secondary indexes (`idx_index_row_groups_depth`, `idx_index_row_groups_user`). The sync also tracks each sort in `footer_cache` (one row each, claimed then marked `hot`). Two scans later `retire` deletes them all again (`d1_delete`, ≤ 5,000 a statement), which D1 also bills as rows written. That is the ~1.5M rows written per scan.

The secondary store (`meta`, `STORES_JSON`) writes 8–21 rows per sort per scan plus 8 age-pyramid rows: 24–50 rows a scan. Not worth touching.

## 2. Readers

### 2.1 Site (`site/functions`)

| Reader | Table | Reached on gcs (`PATH_STORE=intervals`)? |
|---|---|---|
| `openIndex` per-scan branch (`_lib/index.ts`): `schemaRow` → `probe` (`footerCache.ts`) → `d1` / `pq` / `blob` mode; `selectSpans1`, `fetchGroupJson`, `maxDepth`, `readLargest`'s D1 queries | `index_schema`, `index_row_groups`, `footer_cache` | **Only for a date the interval store doesn't hold.** `openIndex` tries `openInterval` first; a held date with an unserved variant is refused (`ivRefuseHeld`), never per-scan. Today the store (`2026-10-09b.g2`: 90 base scans + runs `2026-10-19T0701`, `2026-10-20T0701`, `2026-10-21T0701`, `2026-10-21T1439`) holds **every** one of the 94 primary `path` pointers. So no primary request reads `index_row_groups` |
| `ps=scan` (`withPathStore`): per-scan parity on demand | same | By hand only (no job or page sends it). Retired scans have no files; the newest two read per-scan |
| `perScan(env)`: age pyramids, over-time, search sidecars | same | gcs's primary writes none of those variants (no `age-pyramid-*`, no `over-time`, `SEARCH_SIDECARS` unset): "not synced" whatever D1 holds |
| `/api/diff`, `/api/series`, `/api/subtree`, `/api/largest`, OG cards (`og/serve.ts`), `/api/digest-image` | via `openIndex` | `withPathStore` first: the interval store for held dates |
| `/api/owners`, `/api/estate`, `/api/assignments`, `/api/prefixes`, `/api/owner-prices`, `/api/plans` (`ownerTotals`, `prefixesAt` → `openIndex(slices(env))`) | via `openIndex` | **No `withPathStore`.** In prod the generation is pinned (`INTERVAL_STORE_GEN = "2026-10-09b.g2"` in `site/wrangler.toml`), so `intervalsOn` holds without it and these read the store. On dev (`env.preview`, unpinned) they read per-scan, from D1 rows for the newest two scans (and fail for retired ones, whose files are gone). See §4 |
| `pathScans`, `pathGens`, `pathScanStarts`, `scanArg`, `scanTimes`, `/data` listings, `health`, `/api/dups` | `index_schema` only | Yes, and they stay: (a) keeps writing every pointer |
| `/api/footer-cache` | `footer_cache` (admin stats) | Yes; it reports what's there |
| Footer LRU fill (`footerCache.ts` `queueFill`) | writes `index_row_groups` + `footer_cache` | When a per-scan read finds no rows and no entry, the read is served from the cold footer and a fill of ≤ 200K rows (`FILL_MAX_ROWS`; gcs's sorts are ~82K) is queued under `waitUntil`. On gcs this happens only on the per-scan paths above |

### 2.2 Job (`cloud/src`, `job/`)

| Reader | Needs D1 row groups? |
|---|---|
| `index-sync -p` (`check_flip`) | Yes, today: it refuses unless D1 holds every row group. With `-R` it checks the cold footer instead |
| `index-gc -r 30` (`retire_d1`), `scan retire` (`retire` step), `gc_d1` | Delete them; with none written they delete nothing |
| `publish-r2` (`-a` off: generations an `index_schema` row names), `index-dir`, `index-gc -F` (skipped on gcs), `dups` (`/api/dups` reads `index_schema.dir`; the step reads the `bysize` file), `interval-store append` (reads the sorts from GCS by layout) | No: pointers or files only |
| `healthcheck`, `warm-cache`, `probe -c` | No: they replay site requests, which read the interval store |

### 2.3 The cold footer fallback

- **Complete.** `index-sync -r` writes `<sort>.groups.parquet` (and `.groups.json`) beside each sort before any D1 row: the same rows (`FOOTER_COLS`), 512 per footer group with stats on the pruning columns. `openIndex` opens it (`pq` mode) for any pointer whose generation has no trusted D1 rows, and plans the same rects from it (`pqGroups`). It is how every gcs scan older than `INDEX_RETAIN` (30) was served before the interval store, and the 2026-10-21T1439 generation has all three (15–16 MB each).
- **Speed.** Measured at the path store's rollout (specs/done/path-store.md, 2026-10-01): retired scans' root, drills and lens views in 2.4–3.1 s through the `pq` handle, cold. The footer is range-read and cached (isolate + colo), so a warm view costs what a `d1` one does. And the first `pq` read queues the LRU fill, so a scan read per-scan repeatedly ends up `hot` in D1 anyway.

## 3. Options

| | What | D1 rows written per primary scan | Notes |
|---|---|---|---|
| (a) | `index-sync -R`: no row groups for the primary; pointers and cold footers only | **~0**: 3 pointer upserts, plus `retire` deleting the two `bysize*` pointers of the scan leaving the window: tens of rows | Safe on gcs (§2): nothing reads the primary's row groups for a held date, and the cold footer serves the rest. **Built** |
| (b) | Keep the rows, drop `idx_index_row_groups_depth` and `idx_index_row_groups_user` | about ⅔ fewer index writes, ~1M → still ~0.5M per scan (insert + delete) | Every D1-mode query (`selectSpans1`, `readLargest`) then scans each generation's rows by PK prefix: ~82K rows read per query instead of an index range. Pointless once (a) is on; worth it only for a deployment that must keep per-scan D1 rows |
| (c) | Write only what a fallback needs | — | The fallback needs no D1 rows: the cold footer serves it, and the LRU fill writes rows on demand for a scan read per scan. So (c) is (a) plus the fill that already exists |

## 4. What (a) changes, and its caveats

**Built** (`[cloud]`): `dt-cloud index-sync -R/--no-d1-rows`, both halves:
- `-r -R`: the cold footer files and the sweep of unpointed generations' rows and LRU entries, this generation's included (a partial set from a dead earlier `-r` would otherwise be trusted once pointed). No INSERT, no `footer_cache` entry.
- `-p -R`: per variant, D1 must hold none of the generation's rows (or all of them) and `.groups.parquet` exactly the footer's row groups; any variant failing flips nothing. With a local `-d`, the cold footers must also be under `-b`.
- No flag: both, as `-r -R` then `-p -R`.
- Scan step: `options.index-sync.d1_rows: false` passes `-R` to `index-rows` and `index-sync`. Refused for a secondary store (it never reads the interval store). Default unchanged.

**gcs** (`job/profiles/gcs.yml`): `index-sync: {retain: 30, d1_rows: false}`. Reaches prod with the next image rebuild. `job/run.sh` (the `NF=0` fallback) still syncs rows: a fallback run costs what a scan costs today.

**Caveats:**
1. **A scan the interval store doesn't hold reads per scan.** That happens when an append fails (soft step), and when the site's generation lags the appends (prod's pin while a compaction moves `CURRENT.json`). Answers stay exact (cold footer), first views are ~2.5–3 s slower, and the LRU fill writes that scan's rows on first read (~82K per sort read). Move prod's pin with each compaction, or unpin it.
2. **Owner routes skip `withPathStore`** (`owners`, `estate`, `assignments`, `prefixes`, `owner-prices`, `plans`). Prod's pin hides it. On dev, and in prod once unpinned, they read per-scan: cold footer plus fills for the newest two scans, and an error for older ones (their files are retired). Fix before unpinning prod: route each through `withPathStore` as `subtree`/`diff`/`series` do. Not done here.

## 5. Cleanup (not run; for approval)

- **Primary rows still in D1:** 493,740 `index_row_groups` rows (2026-10-21T0701: 246,180; 2026-10-21T1439: 247,560) and their 6 `footer_cache` entries. Nothing reads them (the interval store holds both scans).
- **Recommended: let `retire` take them.** Once the image with `d1_rows: false` runs, the next two scans push both out of `keep: 2`, and the `retire` step deletes their rows through `d1_delete` (≤ 5,000 a statement) as it already does daily. No manual step, and no new cost: those deletes happen whatever we choose.
- **To drop them now instead** (same cost, two days sooner): `dt-cloud scan retire` over the two scans with `keep` 0 is not supported (`keep ≥ 1`), so it would be `index_footer.d1_delete("index_row_groups", "variant IN ('path','bysize','bysize-user') AND date IN ('2026-10-21T0701','2026-10-21T1439')", …)` then the same `WHERE` on `footer_cache`. The pointers stay.
- **Leave:** the `meta` store's rows (~30 a scan), every `index_schema` row (the scan list reads them).

## 6. Expected D1 rows written per scan after the change

- Primary `index-sync`: 3 `index_schema` upserts (`path`, `bysize`, `bysize-user`; table + PK index each), and the v2 retirement DELETEs, which match nothing: **~10 rows**.
- `retire`: the `bysize` / `bysize-user` pointers of the scan leaving the window, and its rows, which are none from the third scan on: **~5 rows**.
- `meta` store's sync: ~30–50 `index_row_groups` rows + 10 pointers, ×4 index entries: **~200 rows**.
- From ~1.5M to a few hundred, once the two transitional scans have retired (their deletes, §5, are one more ~1M).
