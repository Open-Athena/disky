// POST /api/owner-prices — the pipeline's fill of the owner prefix history (`_lib/ownerHist.ts`,
// specs/owner-pricing-off-the-read-path.md). It writes D1 and spends a history read per call, so it is one named
// grant's alone (the scan job's: `OWNER_PRICES_GRANT`, set in prod's vars only), never a viewer's or an admin's.
//
//   ?n=64[&scan=<id>][&gen=<generation>]  price up to `n` of the ledger's prefixes not priced through the interval
//       store's newest scan, in one read: `{ index, hz, prefixes, priced, remaining, groups, ms }`. Call until
//       `remaining` is 0 (resumable, idempotent; one bounded read a call, so no call nears the Worker's CPU limit).
//       `scan`: 409 `{ behind }` until the store this isolate sees holds that scan (the job asks right after
//       appending it). `gen`: that generation instead of the current one (a generation's first fill).
//   ?carry=<new generation>&from=<generation>  a compaction's gate: `from`'s rows copied under `carry`'s tag, before
//       the store's pointer moves: `{ from, index, hz, carried, remaining, roots }`.
//   ?prune=1  rows of generations the store no longer holds, deleted: `{ kept, dropped }`.
//
// A deployment without an ownership ledger answers `{ ledger: false }` (200): nothing to price.
import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { hasLedger } from '../_lib/ledger.js'
import { carryOwnerHist, fillOwnerHist, pruneOwnerHist } from '../_lib/ownerTotals.js'
import { withPathStore } from '../_lib/index.js'

/** The most a call prices: one history read's row-group budget grows with it. */
const MAX_N = 128
/** A generation's name (a key segment of the store). */
const GEN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/

export const onRequestPost = async (ctx: Ctx): Promise<Response> => {
  if (!ctx.env.OWNER_PRICES_GRANT) return json({ error: 'no owner-prices grant on this deployment' }, 404)
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  if (gated.via !== 'grant' || gated.grantId !== ctx.env.OWNER_PRICES_GRANT) return json({ error: 'forbidden' }, 403)
  const q = new URL(ctx.request.url).searchParams
  const gens = ['gen', 'carry', 'from'].map(k => q.get(k))
  if (gens.some(g => g != null && !GEN.test(g))) return json({ error: 'bad generation' }, 400)
  const [gen, carry, from] = gens
  if ((carry == null) !== (from == null)) return json({ error: 'carry needs from (the generation compacted) and carry (its successor)' }, 400)
  const noStore = { 'cache-control': 'no-store' }
  if (!(await hasLedger(ctx.env))) return json({ ledger: false, priced: 0, remaining: 0 }, 200, noStore)
  const t0 = Date.now()
  // The interval store for the dates it holds (`withPathStore`), not per-scan rows.
  const env = (await withPathStore(ctx)).env
  const none = () => json({ error: 'no interval store serves this deployment' }, 409)
  if (carry && from) {
    const got = await carryOwnerHist(env, from, carry)
    return got ? json({ ...got, ms: Date.now() - t0 }, 200, noStore) : none()
  }
  if (q.get('prune')) {
    const got = await pruneOwnerHist(env)
    return got ? json({ ...got, ms: Date.now() - t0 }, 200, noStore) : none()
  }
  const n = Math.min(MAX_N, Math.max(1, Number(q.get('n') ?? 64) || 64))
  const scan = q.get('scan') ?? undefined
  const got = await fillOwnerHist(env, n, { ...(gen ? { gen } : {}), ...(scan ? { scan } : {}) })
  if (!got) return none()
  if ('behind' in got) return json({ error: `the interval store doesn't hold ${got.behind} yet`, behind: got.behind }, 409, noStore)
  return json({ ...got, ms: Date.now() - t0 }, 200, noStore)
}
