# Owner pricing off the read path

**Status:** option A (the stopgap) is built on `cloud`, not deployed anywhere: no read path prices, the pipeline fills `owner_prefix_hist`, and a compaction carries it before the store's pointer moves. Rolling it out on a deployment with a ledger is that deployment's work (§ Rollout); filling or deleting rows there is a prod D1 write. Option B (the recommendation) is not started (§ What remains for B).

## Problem

A scoped series (`o=` / `lens=`) needs each scan's owner assignments priced: `seriesRead` → `ownerAssignments` → `ownerTotals` → `compute`. `compute` tried `histAggs` (one row per prefix, `owner_prefix_hist`) and otherwise priced per scan with `readAsks`, storing the result in `owner_prefix_aggs`. Both were done inside the request.

Measured on dev.gcs, `g3.c`: a window of 16 unpriced scans used ~32 s of CPU and died as a 1102. The same windows, priced, used 1.0–2.7 s. `owner_prefix_aggs.updated_ts` for that generation lands on exactly the slow minutes (04:48–04:57, 07:36). Each scan was stored as it finished, so retries made progress, which is why these 1102s cleared on their own after enough attempts.

Windowing (`OWNER_HISTORY_BATCH`) bounds decode, not pricing, so it didn't fix this.

`owner_prefix_hist` was empty for current generations because nothing in the pipeline wrote it: its fill was reached only from an admin route, run by hand on 2026-10-14 for `2026-10-09b`. Rows are keyed by `indexTag`, which names the generation (`pq:iv:<gen>:s`), so every new generation started empty, and a `CURRENT` move sent every scoped series back to first-time pricing for all of its scans.

## The rule

**No read path prices.** A read answers from pricing with one bounded read, or returns an explicit not-ready answer. Pricing is written by the pipeline: after a scan is appended, at a ledger write, and before a generation move.

## A: what was built

### Where the fill lives

In the site, behind a job-gated route: `POST /api/owner-prices` (`site/functions/api/owner-prices.ts` → `fillOwnerHist` / `carryOwnerHist` / `pruneOwnerHist` in `_lib/ownerTotals.ts`, over `_lib/ownerHist.ts`). The job is its client (`cloud/src/dt_cloud/owner_prices.py`, `dt-cloud owner-prices`).

Why there and not in Python against D1: a ledger write has to price its new prefix when it lands, and that happens in the site, so the site's pricing (its reader over the interval store's slice sort, `versionsOf`, the tag format) has to exist whatever the job does. A second implementation in the job would be two that can drift. So there is one, and the job only calls it. The cost is that a fill is Worker requests, each bounded by the Worker's CPU limit: so the route prices one chunk a call (64 prefixes, one history read; `n ≤ 128`), is resumable and idempotent (a call prices what D1's rows lack when it runs), and the client halves its chunk when a call fails as a server error, doubling back once it has priced past what the failed call asked for. A prefix refused at every size down to one stops the fill, naming where: the site prices the head of what's stale, and one unpriced prefix leaves the scan's owner views pending and the gate refusing, so pricing past it would buy neither.

The route is one named grant's alone, as `POST /api/plans/staged/card` is: 404 without `OWNER_PRICES_GRANT`, 403 for anyone else (an admin included), set in prod's vars only.

| Call | Does |
|---|---|
| `?n=64[&scan=<id>][&gen=<g>]` | price up to `n` stale prefixes (and the roots) through the store's newest scan: `{ index, hz, prefixes, priced, remaining, groups }`. `scan`: 409 `{ behind }` until the store this isolate sees holds it. `gen`: a generation that isn't current |
| `?carry=<new>&from=<old>` | copy `old`'s rows under `new`'s tag: `{ from, index, hz, carried, remaining, roots }` |
| `?prune=1` | delete the rows of generations the store no longer holds (no `scans.json`): `{ kept, dropped }` |

A deployment without ledger tables answers `{ ledger: false }`.

### When it runs

- **After each scan.** Scan step `owner-prices` (opt-in: `steps: [owner-prices]`; `dt-cloud owner-prices -d <scan> -p`), in `interval-store`'s Nextflow task as its next step, so the fill starts as soon as the append is published. A scan is listed once the store holds it, so until the fill ends (a few minutes) its owner-scoped views answer "pricing pending"; earlier scans are unaffected. The site's view of the store lags its publish by up to two minutes (state held per isolate and per colo), which is what `scan=` and the client's wait are for.
- **At a ledger write.** `POST /api/actions` prices its new prefixes over the store's history in the same request: at most 64 (one read), reported as `pricing: { priced, pending }`. The site's assign flow sends 64 actions a POST (`ASSIGN_CHUNK`), so every gesture is priced by its own writes. A caller that sends more in one POST (the API takes 500) leaves the rest `pending`: owner-scoped reads then answer not-ready until the next fill (the next scan's step, or `dt-cloud owner-prices` by hand).
- **Before a generation move.** See § The gate.

### The read path after

`compute` opens the scan's handle, reads the generation's rows (`SELECT … WHERE idx = ?`: one D1 query per generation, held by the isolate for five minutes, and one more, at most every two seconds, when something isn't priced) and folds. What isn't priced through the scan throws `OwnerPricingPending`: `{ scan, index, why, count, prefixes (≤ 20), roots }`. `HIST_PRICE_MAX`, the per-scan branch and every write to `owner_prefix_aggs` are gone.

- `why: 'pending'`: a fill will price it. Routes answer 503 + `Retry-After: 60` with `{ error, code: 'owner-pricing-pending', pricing }` (`ivRefusal`).
- `why: 'unheld'`: the scan isn't in the interval store, and pricing is per store generation, so no fill is coming: 409. This is a behaviour change for a ledger deployment's scans outside its store, and for a ledger on per-scan stores only: their owner-scoped views no longer answer (they priced per scan in the request, the thing this removes).
- A ledger with no live assignment folds nothing for a lens or a pool, so those never wait. The totals (`/api/owners`) fold the roots, which the fill prices with or without assignments.
- `/api/series` catches it per scan: the scan's point is left out and named (`pricing: { pending, unheld, count, prefixes, roots }`), the body isn't cached, and the chart says so under the line (or in its place when fewer than two scans are priced) and asks again each minute while any is pending.

### The gate (generation moves)

`append_runner.compact_store` and `promote_gen` take a `pricing` gate (`owner_prices.Gate`):

1. Before the fold: the old generation filled to its end (`ready`). A site that can't do that can't carry hours later: refused here, nothing written.
2. In the `pointer` phase, before `successor.json` (so before `CURRENT.json`, whether this run or a later `resolve` would move it): fill again, then **carry** the old generation's rows to the new one's tag, and require nothing unpriced for it. Refused: `SystemExit`, no pointer moved, the old generation stays current for readers and appends, and the new one stays unpublished (the next compaction replaces it, as an interrupted one's).
3. After the pointers (and the R2 copy's) moved: the carry again, best effort, for a prefix a ledger write priced on the old generation meanwhile.

Carry, not re-fill, because the new generation isn't readable by the site when the data bucket's pointer moves (the R2 copy comes after), and because it is exact: a compaction holds the same scans, scan ordinals are the scans' stamps, and `versionsOf` canonicalizes a prefix's versions, so the rows are the same. A test checks it on fixtures: `g2`'s rows carried equal a fill of `g5` (the same scans as a base and a run).

Not asked when `CURRENT.json` names another generation (nothing moves), nor of a candidate (`compact -N`: its `promote` asks).

Wiring: `interval-store add -O`, `interval-store current -s GEN -O` (a hand move is to any generation, so it fills that generation from its own rows: `?gen=`), the staged `compact -O` / `promote -O`, and a staged profile's `append.owner_prices: true` + `append.site_secrets: { "SITE_TOKEN": <secret> }` (the `compact` job then gets `-O`, the submitter's `$SITE_URL` and the token; without either the job isn't submitted). The scan step passes `-O` to the one-machine `add` when the deployment runs `owner-prices`.

**A deployment with no owner assignments is not blocked.** Without ledger tables it doesn't enable any of this; if it did, the site answers `{ ledger: false }` and the gate passes with nothing asked further. With ledger tables and no live assignment, the fill prices the roots only, the carry copies them, nothing is unpriced, and the pointer moves.

### Keys, dev, pruning

Rows are keyed by the generation's tag (`pq:iv:<gen>[@rev][:s]`), so a dev stack pinned to another generation writes and reads its own rows and can't clobber prod's. A dev stack following prod's `CURRENT.json` reads prod's rows (the same store, the same answers) and, at a ledger write, prices the new prefix under that same tag, which prod then needs anyway. A dev stack never fills on a schedule: the route doesn't exist without `OWNER_PRICES_GRANT`, which only prod's vars set.

`?prune=1` (the step's `-p`) deletes every tag whose generation has no `scans.json` in the store, 2,000 rows a statement, the current tag never. So a compacted generation's rows go when the generation does, and a dev experiment's stay while its generation exists. (A dev stack with its own `INTERVAL_STORE_REV` has a tag prod's prune doesn't know, and loses its rows to it.)

`owner_prefix_aggs` is no longer read or written. It stays in the schema for now: dropping it is a D1 migration for each lineage, and until the new site is the only one running a rollback still reads it.

### Cost and growth (gcs)

From the ledger (`GET /api/actions`, 2026-11-05): 1,225 distinct live prefixes, none an object, in 6 buckets.

- **Per scan:** every row's `hz` advances, so every row is rewritten: one per prefix, one per bucket root, one marker, ~1,240 `INSERT OR REPLACE`s in ⌈1,225 / 64⌉ = 20 calls. O(prefixes), not O(changes). Each call also reads the tag's rows and the ledger from D1 (~2,500 rows), so ~50,000 rows read a scan. Both are far inside D1's limits.
- **Per assignment:** one read of the tag's rows, one history read, one row per new prefix.
- **Per compaction:** one statement copying ~1,240 rows.
- **Rows held:** ~1,240 per live generation, rewritten in place: they don't grow with scans, as `owner_prefix_aggs` did (one row per scan and generation, each the whole ledger's pricing).
- **What does grow:** a row's `versions` JSON, by one entry each scan the prefix's totals change. A bucket root changes every scan, so its row grows without bound, and D1's row limit is 2 MB. A row past it fails its chunk's insert, and the fill with it, loudly. Not measured (it needs D1 or a fill on dev); at a few kB a version that is hundreds of scans away, and it is B's to remove.
- **A read's D1 query** is the whole generation's table, not the request's scans: bounded by prefixes × history, held by the isolate.

## Rollout on a deployment with a ledger (gcs)

Each step is the deployment's own (its branch, its D1, its image); none of it is done.

1. **Site, with the grant var.** Deploy with `OWNER_PRICES_GRANT = <the scan job grant's id>` in prod's `[vars]` (not `[env.preview]`). From this deploy on, reads don't price.
2. **First fill**, right away: `dt-cloud owner-prices` with the job's token (about 20 calls). Until it ends, owner-scoped views answer "pricing pending" instead of pricing in the request. To skip that window, fill on the old code first (its admin-only route writes the same rows under the same tag), then deploy.
3. **The step:** `steps: [owner-prices]` in the scan profile, with the site token among the `interval-store` task's secrets; then the image.
4. **Watch one scan:** `/scans` shows `owner-prices` done, and a scoped series over the new scan has no `pricing`.
5. **Only then the compaction:** `append.owner_prices: true` and `append.site_secrets` in the interval-store profile, the Batch service account able to read that secret, `$SITE_URL` in the coordinator's environment, and the hold (`"compact": "none"`) removed.

Out of order:

| What | Then |
|---|---|
| The step before the site | the route refuses the job's grant (the old code's is admin-only): the step warns each scan, nothing else |
| The site without the var | no fill route: every owner-scoped view of every scan is pending, and the step fails saying so |
| The site before a fill | owner views pending until the first fill ends (minutes, if it is run at once) |
| The var on a dev stack | a job pointed at dev would write prod's D1 through it. Prod only |
| The hold lifted before step 3 has run a scan | the gate fills the old generation itself first, so the move is still safe; but no step refreshes it after the next scan |
| The hold lifted without `owner_prices` | an ungated move: every owner view pending (explicitly, not a 1102) until the next scan's fill |
| `owner_prices` without `$SITE_URL` or `site_secrets` | the compaction isn't submitted, each scan, loudly: the hold in effect continues |

## What A doesn't fix

- A scan's owner views are pending between its append and the end of its fill, each scan.
- O(prefixes) rewrites a scan, and rows that grow with history (above).
- A dev stack pinned to another generation is pending until someone fills that generation (`dt-cloud owner-prices -g <gen>` against prod).
- More than 64 new prefixes in one `POST /api/actions` (an agent's, not the site's) leaves the rest pending until the next fill.
- A manual append by another coordinator isn't followed by a fill unless whoever runs it runs `dt-cloud owner-prices` (the step waits ten minutes for the scan, then warns).
- An OG card needing owner totals for a pending scan fails as any unreadable view does.

## What remains for B

The rule holds either way; B changes where pricing lives.

| | A (built) | B: pricing as a served file in the interval store |
|---|---|---|
| Written by | the site's fill, called by the job | the store's append / carry / compaction, as `attribution` is |
| Read | one D1 query per generation, held by the isolate | one bounded range read over a `(path, vf)` sort |
| Cost of a scan | O(prefixes) row rewrites | O(changes): only prefixes whose totals changed get a version |
| Growth | rows grow with history, toward D1's row limit | one row per version, no limit |
| New prefix | priced by its write, in the site | the same read in a job, or a not-ready answer until it runs |
| Generation move | carried by a gate | built with the generation, so a move can't outrun it |
| Dev sharing prod's D1 | dev's ledger writes still write prod rows | no D1 writes |

To build B: a pricing sort cut by the store's stages (append, carry, compaction, backfill for existing generations), keyed `(depth, path, vf)` over the ledger's prefixes and the roots; the reader's range read in place of `histAggs`' D1 query (`OwnerPricingPending` and everything above it stay); a job step that prices a ledger write's prefixes between scans, or the write's own read kept as the bridge; then drop the route, the gate, `owner_prefix_hist` and `owner_prefix_aggs`.

## Open questions

- The `g3.c` rows dev tests added to `owner_prefix_aggs`, and that table itself: keep or delete is Ryan's call (a D1 write).
- Prod's 1102s are not yet tied to pricing by route: a read-only error tail was collecting that.
- How large the busiest rows' `versions` are today.
