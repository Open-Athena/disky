/** Per-user owned bytes for a scan — the ownership ledger folded server-side
 * over each live assignment prefix's bytes (specs/done/path-agnostic-serving.md
 * §2.3), so every consumer (`/users`, `/user/:id`, `/api/assignments`, the
 * user lens of `/api/subtree`) reads one number.
 *
 * No read path prices (specs/owner-pricing-off-the-read-path.md). A prefix's bytes
 * on every scan of an interval-store generation are one D1 row (`owner_prefix_hist`,
 * `ownerHist.ts`), filled by the pipeline: the scan job's `owner-prices` step once a
 * scan is appended, the ledger write that adds a prefix (`priceAssigned`), and a
 * compaction carrying a generation's rows to its successor before the store's pointer
 * moves (`carryOwnerHist`). A fold reads those rows in one query per generation (held
 * by the isolate) and folds the ledger over them (~0.1 s, memoized per (scan, head) in
 * the isolate: an owner change writes nothing). What isn't priced through the scan is
 * an explicit not-ready answer (`OwnerPricingPending`: which prefixes, and why), never
 * priced inside the request: pricing a history's worth of scans there cost ~2 s of CPU
 * a scan, and a window of 16 died at the Worker's limit. */
import type { Env } from './auth.js'
import { ivGenerations, ivHandleGen, ivHeldDates, openIndex, pathStoreKey, slices, type IndexHandle } from './index.js'
import { carryHist, dropHist, fillHist, histAggs, histStale, histTags } from './ownerHist.js'
import { type Ledger, loadLedger } from './ledger.js'
import { OwnerPricingPending, PENDING_SAMPLE, WRITE_PRICE_MAX } from './pricingPending.js'
import { joinOf, shared } from './shared.js'
import { computeOwners, foldLatest, idxKey, type AssignmentRow, type OwnerRow, type OwnerTotals } from './ownerBands.js'

/** Bump when the body's shape changes. */
const MANIFEST_VERSION = 4  // 2: `claims` → `assignments`; 3: per-user objects (`uo`); 4: `computed` names the index priced on

export interface OwnerTotalsBody extends OwnerTotals {
  v: number
  scan: string
  head: number
  /** `index`: the tag of the pricing the fold read (a store generation's). */
  computed: { at: number; ms: number; index: string }
}

// Per-isolate memo of the fold, per path store (a generation's fold is its own pricing's: after the store's pointer
// moves, a scan folds again, from the new generation's rows). A stalled one is evicted after a minute.
const memo = new Map<string, Promise<OwnerTotalsBody>>()
const MEMO_WAIT = 60_000

/** The index a scan's prefixes are priced on: a new store generation is priced anew (or carried to). */
const indexTag = (idx: { mode: string }): string => `${idx.mode}:${'gen' in idx ? (idx as { gen: string }).gen : ''}`

/** The ledger's prefixes as index lookups: path → depth. */
function wantOf(owners: Map<string, OwnerRow> | Iterable<string>): Map<string, number> {
  const want = new Map<string, number>()
  for (const p of owners instanceof Map ? owners.keys() : owners) {
    const { path, depth } = idxKey(p)
    want.set(path, depth)
  }
  return want
}

async function compute(env: Env, date: string, owners: Map<string, OwnerRow>, head: number): Promise<OwnerTotalsBody> {
  const t0 = Date.now()
  const idx = await openIndex(slices(env), date)
  const tag = indexTag(idx)
  const want = wantOf(owners)
  // Pricing is filled per interval-store generation: a scan read from per-scan stores has none, and gets none.
  if (idx.asOf == null) throw new OwnerPricingPending({ scan: date, index: tag, why: 'unheld', count: want.size, prefixes: [...want.keys()].sort().slice(0, PENDING_SAMPLE), roots: true })
  const hist = await histAggs(slices(env), tag, idx.asOf, want)
  if ('stale' in hist) {
    throw new OwnerPricingPending({ scan: date, index: tag, why: 'pending', count: hist.stale.paths.length, prefixes: hist.stale.paths.slice(0, PENDING_SAMPLE), roots: hist.stale.roots })
  }
  const totals = computeOwners({ owners, aggs: hist.aggs, buckets: hist.buckets })
  return { v: MANIFEST_VERSION, scan: date, head, ...totals, computed: { at: Math.floor(Date.now() / 1000), ms: Date.now() - t0, index: tag } }
}

/** The owner totals for `date` at the current ledger head: folded from the generation's priced prefixes (one
 * bounded D1 read, held by the isolate), memoized per (scan, head) in the isolate. Throws `OwnerPricingPending`
 * (a not-ready answer: `ivRefusal`) for what the pipeline hasn't priced; it prices nothing. */
export async function ownerTotals(env: Env, date: string): Promise<OwnerTotalsBody> {
  return totalsAt(env, date, await loadLedger(env))
}

const totalsAt = (env: Env, date: string, { ownerRows, head }: Ledger): Promise<OwnerTotalsBody> =>
  shared(memo, `${pathStoreKey(slices(env))}|${date}:${head}`, () => compute(env, date, foldLatest(ownerRows), head), MEMO_WAIT, joinOf(env, 'owner-totals'))

/** The live assignments priced against `date` — what a user lens folds. None when the ledger holds none (the lens
 * is then the scan's attribution: nothing to price, so nothing can be pending). */
export async function ownerAssignments(env: Env, date: string): Promise<AssignmentRow[]> {
  const ledger = await loadLedger(env)
  return ledger.ownerRows.length ? (await totalsAt(env, date, ledger)).assignments : []
}

// --- the fill (the pipeline's writes: `POST /api/owner-prices`, a ledger write) -----------------------------------

/** `env` reading generation `gen` of its interval store (a pin for this call), else as it is. */
const atGen = (env: Env, gen?: string): Env => (gen ? { ...env, PATH_STORE: 'intervals', INTERVAL_STORE_GEN: gen } : env)

/** The interval store's newest handle (its tag is the generation's pricing key); null when no store serves `env`. */
async function newestHandle(env: Env): Promise<{ idx: IndexHandle; tag: string; dates: string[] } | null> {
  const dates = await ivHeldDates(slices(env))
  if (!dates.length) return null
  const idx = await openIndex(slices(env), dates[dates.length - 1])
  return { idx, tag: indexTag(idx), dates }
}

export interface Fill {
  /** The index tag priced (`pq:iv:<gen>…`). */
  index: string
  /** The scan ordinal priced through (the store's newest scan's). */
  hz: number
  /** The ledger's prefixes (or `only`'s). */
  prefixes: number
  priced: number
  remaining: number
  groups: number
}

/** Fill the history rows for the ledger's prefixes (or `only`: these ledger prefixes) not priced through the
 * interval store's newest scan, `n` per call (one history read each): after a scan is appended, after a ledger
 * write, and a generation's first fill (`gen`: that generation, not the current one). Resumable and idempotent:
 * call until `remaining` is 0. `scan`: refuse (`behind`) until the store holds it, so a fill asked for a scan the
 * site hasn't seen yet (its state is held a minute) doesn't report an older horizon as done. Null: no interval
 * store serves this deployment. */
export async function fillOwnerHist(env: Env, n: number, { gen, scan, only }: { gen?: string; scan?: string; only?: string[] } = {}): Promise<Fill | { behind: string } | null> {
  const e = atGen(env, gen)
  const at = await newestHandle(e)
  if (!at) return null
  if (scan && !at.dates.includes(scan)) return { behind: scan }
  const want = only ? wantOf(only) : wantOf(foldLatest((await loadLedger(env)).ownerRows))
  const got = await fillHist(slices(e), at.tag, want, n)
  return got && { index: at.tag, prefixes: want.size, ...got }
}

/** A ledger write's follow-up: its prefixes priced over the store's history in one read, so the next fold is ready.
 * `pending`: what it left for the next fill (more than `WRITE_PRICE_MAX` new prefixes). Null: no interval store
 * serves this deployment, so there is no pricing to fill. */
export async function priceAssigned(env: Env, prefixes: string[]): Promise<{ priced: number; pending: number } | null> {
  const got = await fillOwnerHist(env, WRITE_PRICE_MAX, { only: prefixes })
  return got && !('behind' in got) ? { priced: got.priced, pending: got.remaining } : null
}

/** A compaction's gate: the current generation `from`'s rows copied under `to`'s tag (the same scans, so the same
 * versions), then what of the ledger `to` still doesn't price through `from`'s newest scan. Null: no interval
 * store serves `from`. */
export async function carryOwnerHist(env: Env, from: string, to: string): Promise<{ from: string; index: string; hz: number; carried: number; remaining: number; roots: boolean } | null> {
  const at = await newestHandle(atGen(env, from))
  if (!at || at.idx.asOf == null) return null
  // `to`'s tag is `from`'s with the generation swapped (the same revision and slicing: a compaction keeps both).
  const sliced = (at.idx as { gen: string }).gen === ivHandleGen(env, from, true)
  const tag = indexTag({ mode: at.idx.mode, gen: ivHandleGen(env, to, sliced) } as { mode: string })
  const carried = await carryHist(env, at.tag, tag)
  const want = wantOf(foldLatest((await loadLedger(env)).ownerRows))
  return { from: at.tag, index: tag, hz: at.idx.asOf, carried, ...(await histStale(env, tag, want, at.idx.asOf)) }
}

/** Rows of generations the store no longer holds, deleted: every tag but those of a generation with a `scans.json`
 * (the current one always kept). Null: no interval store, or its bucket can't be listed (nothing deleted). */
export async function pruneOwnerHist(env: Env): Promise<{ kept: string[]; dropped: { index: string; rows: number }[] } | null> {
  const at = await newestHandle(env)
  const gens = at && await ivGenerations(env)
  if (!at || !gens) return null
  const keep = new Set([at.tag, ...gens.flatMap(g => [true, false].map(s => indexTag({ mode: at.idx.mode, gen: ivHandleGen(env, g, s) } as { mode: string })))])
  const dropped: { index: string; rows: number }[] = []
  const kept: string[] = []
  for (const { idx } of await histTags(env)) {
    if (keep.has(idx)) kept.push(idx)
    else dropped.push({ index: idx, rows: await dropHist(env, idx) })
  }
  return { kept, dropped }
}
