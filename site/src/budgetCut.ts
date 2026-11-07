// A filter view or diff whose phase 2 its time budget cut short (`budgetCut`: e.g. a heavy literal's nested
// rollup on a cold isolate, the fleet root drawn to bucket → top dirs only) is exact in its totals but partial
// in its drawn interiors; the server computes the whole answer in the background and replaces it at the edge
// (`upgradePartial`, functions/_lib/edgeCache.ts). The client's queries are `staleTime: Infinity`, so without
// asking again the cut answer stays on screen for the session: ask again until the answer is whole. When to ask
// is the server's to say: a cut answer's `Retry-After` is when the run it started (or found running) should be
// done. Without one (no run on: the server gave up on the key until its partial expires, or an older deploy),
// every `REASK_FALLBACK_MS`. Either way at most `REASK_MAX` times and not past `REASK_TOTAL_MS` from the first cut
// answer.

import { hashKey } from '@tanstack/react-query'

/** Re-asks of one query at most. */
export const REASK_MAX = 10
/** No re-ask later than this after the query's first cut answer, ms. */
export const REASK_TOTAL_MS = 60_000
/** The spacing when an answer says no `Retry-After`, ms (at most 6 re-asks under `REASK_TOTAL_MS`). */
export const REASK_FALLBACK_MS = 10_000
/** A `Retry-After` is held to this range, ms. */
export const RETRY_AFTER_MIN_MS = 1_000
export const RETRY_AFTER_MAX_MS = 20_000

/** Each query's latest `Retry-After`, ms, by its hash (`noteRetryAfter`). By query, not by answer: react-query's
 *  structural sharing keeps the held answer object when a re-ask returns the same one, so a note on the answer
 *  would be the first response's. */
const retryAfters = new Map<string, number>()
/** Per query (react-query's `Query`, the same object across its fetches): when its first cut answer was seen. */
const cutSince = new WeakMap<object, number>()

/** Note the `Retry-After` (delta seconds; none: forget it) of the response the query `queryKey` just fetched, for
 *  `budgetCutRefetch`. */
export function noteRetryAfter(queryKey: readonly unknown[], headers: Headers): void {
  const h = hashKey(queryKey), s = Number(headers.get('retry-after'))
  if (headers.has('retry-after') && Number.isFinite(s) && s >= 0) retryAfters.set(h, s * 1000)
  else retryAfters.delete(h)
}

/** react-query's `refetchInterval` for a view query `q`: its held answer (`state.data`) and how many times it was
 *  fetched (`state.dataUpdateCount`). */
export function budgetCutRefetch(q: { queryHash: string; state: { data?: { budgetCut?: boolean }; dataUpdateCount: number } }, now = Date.now()): number | false {
  const { data, dataUpdateCount } = q.state
  if (!data?.budgetCut) {
    cutSince.delete(q)
    retryAfters.delete(q.queryHash)
    return false
  }
  const since = cutSince.get(q) ?? now
  cutSince.set(q, since)
  if (dataUpdateCount > REASK_MAX) return false
  const said = retryAfters.get(q.queryHash)
  const wait = said == null ? REASK_FALLBACK_MS : Math.min(RETRY_AFTER_MAX_MS, Math.max(RETRY_AFTER_MIN_MS, said))
  return now - since + wait <= REASK_TOTAL_MS ? wait : false
}
