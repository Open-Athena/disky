/** A read that needs owner pricing the pipeline hasn't filled (specs/owner-pricing-off-the-read-path.md): no read
 * path prices, so it answers "not ready" — which prefixes, and why — instead of pricing inside the request. */

/** `pending`: the scan's prefixes (or roots) aren't priced through it yet; the fill (`POST /api/owner-prices`: the
 *  scan job's `owner-prices` step, or the ledger write that added the prefix) will. `unheld`: the interval store
 *  doesn't hold the scan, and pricing is filled per store generation only: no fill is coming. */
export type PricingWhy = 'pending' | 'unheld'

export interface PricingPending {
  scan: string
  /** The index the scan is priced on (`<mode>:<gen>`; a store generation's tag). */
  index: string
  why: PricingWhy
  /** How many of the ledger's prefixes aren't priced through `scan`. */
  count: number
  /** The first `PENDING_SAMPLE` of them (index paths, sorted). */
  prefixes: string[]
  /** The depth-1 roots (every bucket's row) aren't priced through `scan`. */
  roots: boolean
}

/** The most prefixes a ledger write prices in its own request (`POST /api/actions`): one history read, the fill's
 *  chunk. The site's own assign flow sends at most this many per POST (`src/batches.ts`), so each is priced at once. */
export const WRITE_PRICE_MAX = 64
/** The most prefixes a not-ready answer names. */
export const PENDING_SAMPLE = 20
/** A not-ready answer's `Retry-After` (seconds): a fill chunk takes a few seconds, a scan's whole fill a few minutes. */
export const PENDING_RETRY = 60
/** The answer's `code` (a client tells it from any other 503 by it). */
export const PENDING_CODE = 'owner-pricing-pending'

const message = (p: PricingPending): string => {
  if (p.why === 'unheld') return `owner totals aren't available for scan ${p.scan}: the interval store doesn't hold it, and owner pricing is filled per store generation`
  const what = [p.count ? `${p.count} assigned ${p.count === 1 ? 'prefix' : 'prefixes'}` : '', p.roots ? 'the store roots' : ''].filter(Boolean).join(' and ')
  return `owner pricing is pending for scan ${p.scan}: ${what} not priced through it yet (filled by the scan job's owner-prices step, or by the assignment that added a prefix)`
}

export class OwnerPricingPending extends Error {
  constructor(readonly pending: PricingPending) {
    super(message(pending))
    this.name = 'OwnerPricingPending'
  }
}

/** The answer's JSON body: `{ error, code, pricing }`. */
export const pricingBody = (e: OwnerPricingPending): { error: string; code: string; pricing: PricingPending } => ({ error: e.message, code: PENDING_CODE, pricing: e.pending })

/** The not-ready answer (`OwnerPricingPending`): a 503 + `Retry-After` while a fill is pending, a 409 for a scan no
 *  fill will price; null for any other error. Never cached (`no-store`). */
export function pricingRefusal(e: unknown): Response | null {
  if (!(e instanceof OwnerPricingPending)) return null
  const pending = e.pending.why === 'pending'
  return new Response(JSON.stringify(pricingBody(e)), {
    status: pending ? 503 : 409,
    headers: { 'content-type': 'application/json', 'cache-control': 'no-store', ...(pending ? { 'retry-after': String(PENDING_RETRY) } : {}) },
  })
}
