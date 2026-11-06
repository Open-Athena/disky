/**
 * Test-only: owner totals priced per scan, inside the call — what `ownerTotals` did on its read path before
 * pricing moved to the pipeline (specs/owner-pricing-off-the-read-path.md): one point lookup per live assignment
 * prefix plus the depth-1 roots on the scan's own index, then the ledger folded over them. Kept as the oracle the
 * fill is checked against; nothing outside `*.test.ts` imports this file.
 */
import type { Env } from './auth.js'
import { columnsFor, openIndex, readAsks, slices, type Ask, type Row } from './index.js'
import { ledgerHead } from './ledger.js'
import { addAgg, computeOwners, foldLatest, idxKey, newAgg, type OwnerRow, type OwnerTotals, type PathAgg } from './ownerBands.js'

const FIELDS: (keyof Row)[] = ['path', 'depth', 'usr', 'size', 'n_files', 'cls2', 'cls3', 'cls4']

type Real = typeof import('./ownerTotals.js')

/** `ownerTotals.ts` for a suite whose oracle is the per-scan reader (`vi.mock('./ownerTotals', () =>
 *  perScanOracle(() => vi.importActual('./ownerTotals')))`): a scan no interval store holds (the refusal `unheld`:
 *  pricing is filled per store generation) folds from `perScanTotals` instead, so the per-scan store's lens views,
 *  pools and totals stand as what the interval store's are compared to. Everything else is the module's own,
 *  looked up at each call (`actual`): a mock's factory runs once, so a suite that resets its modules for a cold
 *  isolate still gets the module's fresh instance. */
export function perScanOracle(actual: () => Promise<Real>): Real {
  // By name: a suite that resets its modules holds more than one instance of the error's class.
  const unheld = (e: unknown): boolean => (e as { name?: string; pending?: { why?: string } } | null)?.name === 'OwnerPricingPending' && (e as { pending: { why: string } }).pending.why === 'unheld'
  const or = async <T>(p: Promise<T>, alt: () => Promise<T>): Promise<T> => {
    try {
      return await p
    } catch (e) {
      if (!unheld(e)) throw e
      return alt()
    }
  }
  return {
    ownerTotals: async (env, date) => or((await actual()).ownerTotals(env, date), async () => ({ v: 0, ...(await perScanTotals(env, date)), computed: { at: 0, ms: 0, index: 'per-scan' } })),
    ownerAssignments: async (env, date) => or((await actual()).ownerAssignments(env, date), async () => (await perScanTotals(env, date)).assignments),
    fillOwnerHist: async (...a) => (await actual()).fillOwnerHist(...a),
    priceAssigned: async (...a) => (await actual()).priceAssigned(...a),
    carryOwnerHist: async (...a) => (await actual()).carryOwnerHist(...a),
    pruneOwnerHist: async (...a) => (await actual()).pruneOwnerHist(...a),
  }
}

/** `date`'s owner totals at the current ledger head, priced on that scan's index in this call. */
export async function perScanTotals(env: Env, date: string): Promise<OwnerTotals & { scan: string; head: number }> {
  // The ledger straight from `env`'s D1 (`ledger.ts`' rows, without its per-isolate memo: a suite seeds several
  // databases whose heads coincide).
  const head = await ledgerHead(env)
  const ownerRows = (await env.DB!.prepare(
    "SELECT o.prefix, COALESCE(o.kind, 'prefix') AS kind, o.owner, o.ts, a.actor AS who, a.id AS action_id " +
    'FROM owner_prefixes o JOIN actions a ON a.id = o.action_id WHERE o.tombstoned IS NULL',
  ).all<OwnerRow>()).results
  const owners = foldLatest(ownerRows)
  const idx = await openIndex(slices(env), date)
  const want = new Map<string, number>()
  for (const p of owners.keys()) {
    const { path, depth } = idxKey(p)
    want.set(path, depth)
  }
  const asks: Ask[] = [{ depth: 1, under: '' }, ...[...want].map(([path, depth]) => ({ depth, path }))]
  const read = await readAsks(idx, asks, r => r.depth === 1 || want.get(r.path) === r.depth, { columns: columnsFor(idx, FIELDS), maxGroups: Math.max(400, 2 * asks.length) })
  const aggs = new Map<string, PathAgg>()
  const buckets = new Set<string>()
  for (const r of read.rows) {
    if (r.depth === 1) buckets.add(r.path)
    let a = aggs.get(r.path)
    if (!a) aggs.set(r.path, (a = newAgg()))
    addAgg(a, r)
  }
  return { scan: date, head, ...computeOwners({ owners, aggs, buckets: [...buckets].sort() }) }
}
