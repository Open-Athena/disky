/**
 * `GET /api/filter-scans` — the scans the static name index covers (`staticFilter.ts`'s store, the same list
 * `indexedGate` checks), so a filtered view with no explicit `?d=` can open on the newest of them before it
 * fetches anything, instead of on a just-published scan the index hasn't appended yet. `store=<key>` answers
 * for that store's own index (none of its own, under the static filter: `[]`).
 *
 *   → { scans: ["2026-10-08", "2026-10-09"] }   ascending; null when the static filter is off
 */
import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { staticFilterStore, type StaticFilterEnv } from '../_lib/staticFilter.js'
import { withStore } from '../_lib/stores.js'

export const onRequestGet = async (ctx0: Ctx): Promise<Response> => {
  const ctx = withStore(ctx0)
  if (ctx instanceof Response) return ctx
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  const s = staticFilterStore(ctx.env as unknown as StaticFilterEnv)
  const scans = s ? [...await s.scans()].sort() : null
  return json({ scans }, 200, { 'cache-control': 'private, max-age=60' })
}
