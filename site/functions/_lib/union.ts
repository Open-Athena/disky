/** Union views (specs/federated-scans.md, "First live cases" 1): one
 * deployment's stores read side by side at request time.
 *
 * A union view (`STORES_JSON` entry `{"union": [<store>…], "main"?: true}`,
 * `stores.ts`) names member stores of the same deployment. A request on it
 * resolves **one cutoff** (the scan the request names, in whichever member
 * indexes it), then each member's scan at that cutoff — the latest it
 * observed through it (`latestObserved`, the federated resolver's rule), so
 * members whose scan ids happen to align pick the same id and members that
 * scan on their own schedules carry their last scan forward. A member with
 * no scan by the cutoff is unknown (`date: null`), never its first later
 * scan.
 *
 * The union's root is its members' roots side by side; any other path
 * belongs to the one member whose roots hold its first segment, and is
 * answered by that member's own read path (`store=<member>`, `withStore`) at
 * its resolved scan — so its cache keys are the member's own, carrying that
 * scan. Members' roots must be disjoint (a shared root is a config error,
 * never a sum).
 *
 * Nothing here names a deployment or a store: the config drives it. A
 * deployment without a union view never reaches this module.
 */
import { decodeScan, isScanId, scanInstant } from '../../src/scanSlug.js'
import { compareScans, latestObserved, type Observation } from '../../src/federatedScans.js'
import { type Env, json, type Identity, requireViewer } from './auth.js'
import { ivRetry, pathScans, storeReady, withPathStore } from './index.js'
import { indexedScan, noScan } from './scanArg.js'
import { scanTimes } from './scanTimes.js'
import { PRIMARY_STORE, StoreConfigError, type UnionView, unionViews, withStore } from './stores.js'
import { readRootRows, type ViewNode } from './view.js'

type Ctx = { request: Request; env: Env; waitUntil?: (p: Promise<unknown>) => void }

/** The union view a request reads, if any: `store=<view key>`, or no `store=`
 * on a deployment with a main view. `store=primary` (and any store key) is
 * that store alone. A malformed `STORES_JSON` is a 500. */
export function unionFor(request: Request, env: Env): UnionView | null | Response {
  if (!env.STORES_JSON) return null
  let views: Record<string, UnionView>
  try {
    views = unionViews(env)
  } catch (e) {
    if (e instanceof StoreConfigError) return json({ error: e.message }, 500)
    throw e
  }
  const raw = new URL(request.url).searchParams.get('store')
  if (raw === null || raw === '') return Object.values(views).find(v => v.main) ?? null
  return views[raw] ?? null
}

/** `request` as member `store` asks it: `store=<member>` (none for the
 * primary), `set` params replaced, `drop` params removed. Headers (the
 * session cookie), method and signal carry over. */
export function memberRequest(request: Request, store: string, set: Record<string, string> = {}, drop: string[] = []): Request {
  const url = new URL(request.url)
  if (store === PRIMARY_STORE) url.searchParams.delete('store')
  else url.searchParams.set('store', store)
  for (const k of drop) url.searchParams.delete(k)
  for (const [k, v] of Object.entries(set)) url.searchParams.set(k, v)
  return new Request(url, request)
}

/** One member of a resolved union: its context (the store's env overlay, as
 * `withStore` gives it) and its scan at the cutoff (null = unknown). */
export interface Member {
  store: string
  ctx: Ctx
  date: string | null
}

/** A union resolved at one cutoff: the scan the request named (`date`, the
 * client's as-of), its instant (`at`), and every member's scan at it. */
export interface Resolved {
  view: UnionView
  date: string
  at: string
  members: Member[]
}

/** The `union` block a response carries: the per-store vector it was read at. */
export const vectorOf = (r: Resolved): { view: string; at: string; sources: { store: string; date: string | null }[] } =>
  ({ view: r.view.key, at: r.at, sources: r.members.map(m => ({ store: m.store, date: m.date })) })

/** Each member's context for `request` (`withStore` + `withPathStore`, as a
 * single-store handler builds its own). */
function memberCtxs(ctx: Ctx, view: UnionView): Ctx[] | Response {
  const out: Ctx[] = []
  for (const store of view.members) {
    const c = withStore({ ...ctx, request: memberRequest(ctx.request, store) })
    if (c instanceof Response) return c
    out.push(withPathStore(c))
  }
  return out
}

const iso = (ms: number): string => new Date(ms).toISOString()

/** A member's indexed scans as observations: each id observed through its
 * own instant (`scanInstant`: a timed id's minute, a date-only id's recorded
 * start, else its midnight). One D1 query for the ids, one for the starts. */
async function observations(env: Env): Promise<Observation[]> {
  const dates = (await pathScans(env, true)).results.map(r => r.date)
  const times = await scanTimes(env, dates)
  return dates.map(id => { const t = iso(scanInstant(id, times)); return { id, revision: '', observed_through: t, published_at: t } })
}

/**
 * Resolve the request's cutoff and every member's scan at it. `date=<id>`:
 * an exact scan id, which some member must index (else the same 404 a
 * single store answers); `d=<slug>`: the latest scan any member indexes
 * under the slug. Then each member's latest scan observed through that
 * instant. Every member is gated first (`requireViewer` on its own env: a
 * union demands each member's scope), so nothing is read for a viewer who
 * may not see all of it.
 */
export async function resolveUnion(ctx: Ctx, view: UnionView, gate: boolean): Promise<{ resolved: Resolved; id: Identity | null } | Response> {
  const ctxs = memberCtxs(ctx, view)
  if (ctxs instanceof Response) return ctxs
  for (const c of ctxs) {
    if (!c.env.DB) return json({ error: 'index backend not configured (DB)' }, 503)
    if (!storeReady(c.env)) return new Response('subtree API not configured (missing index store creds)', { status: 503 })
  }
  let id: Identity | null = null
  if (gate) {
    for (const c of ctxs) {
      const g = await requireViewer(c as never)
      if (g instanceof Response) return g
      id ??= g
    }
  }
  const sp = new URL(ctx.request.url).searchParams
  const obs = await Promise.all(ctxs.map(c => observations(c.env)))
  let date: string
  let at: string
  const raw = sp.get('date')
  if (raw !== null || !sp.has('d')) {
    if (!raw || !isScanId(raw)) return json({ error: 'date=YYYY-MM-DD[THHMM] required' }, 400)
    const hit = obs.map(o => o.find(s => s.id === raw)).find(Boolean)
    if (!hit) return noScan('date', raw)
    date = raw
    at = hit.observed_through
  } else {
    const slug = sp.get('d') ?? ''
    const prefix = decodeScan(slug)
    const found = prefix ? await Promise.all(ctxs.map(c => indexedScan(c.env, prefix, false))) : []
    const cands = found.map((f, i) => (f ? obs[i].find(s => s.id === f) ?? null : null)).filter((s): s is Observation => !!s)
    const best = cands.sort(compareScans).at(-1)
    if (!best) return noScan('d', slug)
    date = best.id
    at = best.observed_through
  }
  const members = view.members.map((store, i) => ({ store, ctx: ctxs[i], date: latestObserved(obs[i], at)?.id ?? null }))
  return { resolved: { view, date, at, members }, id }
}

/** A path's member: the one whose roots (at its resolved scan) hold the
 * path's first segment; null = none does (a 404). Two members holding it is
 * a misconfigured union (overlapping scopes), thrown. */
export async function memberOf(r: Resolved, path: string): Promise<Member | null> {
  const seg = path.split('/')[0]
  const ready = r.members.filter(m => m.date !== null)
  const roots = await Promise.all(ready.map(m => ivRetry(() => readRootRows(m.ctx.env, m.date!))))
  const owners = ready.filter((_, i) => (roots[i] ?? []).some(row => row.path === seg))
  if (owners.length > 1) throw new UnionOverlap(`union view '${r.view.key}': root '${seg}' is in stores ${owners.map(m => m.store).join(', ')}`)
  return owners[0] ?? null
}

/** Two members hold the same root: the union would double count it. */
export class UnionOverlap extends Error {}

/** The union's root from its members' root trees (each read under the union's threshold): every member's
 *  top-level nodes, tagged with its store (`src`) and scan (`at`), largest first; the members' `(other)`s
 *  summed into one, last; the totals summed under `label`. A top-level name two members draw is a
 *  misconfigured union (`UnionOverlap`), never a sum. */
export function unionRoot(view: UnionView, label: string, members: readonly { store: string; date: string; tree: ViewNode }[]): ViewNode {
  const kids: ViewNode[] = []
  const other: ViewNode = { n: '(other)', k: 'dir', b: 0, o: 0, f: 0 }
  const seen = new Map<string, string>()
  for (const m of members) {
    for (const c of m.tree.c ?? []) {
      if (c.n === '(other)') {
        other.b += c.b; other.o += c.o; other.f = (other.f ?? 0) + (c.f ?? 0)
        continue
      }
      const prev = seen.get(c.n)
      if (prev) throw new UnionOverlap(`union view '${view.key}': root '${c.n}' is in stores ${prev}, ${m.store}`)
      seen.set(c.n, m.store)
      kids.push({ ...c, src: m.store, at: m.date })
    }
  }
  kids.sort((x, y) => y.b - x.b)
  if (other.b > 0 || other.o > 0) kids.push(other)
  return {
    n: label, k: 'dir',
    b: members.reduce((n, m) => n + m.tree.b, 0), o: members.reduce((n, m) => n + m.tree.o, 0),
    ...(kids.length ? { c: kids } : {}),
  }
}
