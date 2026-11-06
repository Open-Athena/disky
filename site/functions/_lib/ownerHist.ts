/** Assignment prefixes priced over an interval store's whole history (specs/owner-prefix-history.md): one D1 row
 * per prefix (`owner_prefix_hist`) holding its aggregate's versions over scan ordinals, as the store holds its
 * rows. An owner change reads and writes nothing; a new prefix is one history read and one row, however many
 * scans the store holds; a scan's fold takes each prefix's version live at that scan.
 *
 * The rows are filled by the pipeline, never by a read (specs/owner-pricing-off-the-read-path.md): `fillHist`
 * (`POST /api/owner-prices`: the scan job's `owner-prices` step once a scan is appended; the ledger write that adds
 * a prefix) and `carryHist` (a compaction's new generation, before the store's pointer moves to it). A fold
 * (`histAggs`) reads them in one query per generation, held by the isolate, and says what isn't priced. */
import type { Env } from './auth.js'
import { ivHeldDates, ivHorizon, readAsksHistory, type Ask, type Row } from './index.js'
import { addAgg, newAgg, type PathAgg } from './ownerBands.js'
import { inFlight, joinOf, shared } from './shared.js'

/** A prefix's aggregate over scan ordinals `[vf, vt)`. */
export type Version = [number, number, PathAgg]
interface Hist { hz: number; versions: Version[] }

/** The roots marker's key: every depth-1 path is priced through its `hz`. */
const ROOTS = '0:'
const key = (depth: number, path: string) => `${depth}:${path}`

/** An aggregate with its maps' keys sorted, so equal aggregates serialize alike. */
const canon = (a: PathAgg): string => JSON.stringify([a.b, a.o, ...[a.us, a.uo, a.cb].map(m => Object.entries(m).sort(([x], [y]) => x < y ? -1 : x > y ? 1 : 0))])

/** Each `(depth, path)`'s versions from interval-store version rows (one per owner slice, each live over its own
 *  `[vf, vt)`): the history cut at every slice bound, each segment's live slices summed, equal neighbours merged.
 *  A segment with no live slice is a gap (the path isn't in those scans). */
export function versionsOf(rows: Row[]): Map<string, Version[]> {
  const groups = new Map<string, Row[]>()
  for (const r of rows) {
    const k = key(r.depth, r.path)
    const g = groups.get(k)
    if (g) g.push(r)
    else groups.set(k, [r])
  }
  const out = new Map<string, Version[]>()
  for (const [k, g] of groups) {
    g.sort((a, b) => (a.usr ?? '') < (b.usr ?? '') ? -1 : (a.usr ?? '') > (b.usr ?? '') ? 1 : a.vf! - b.vf!)
    const bounds = [...new Set(g.flatMap(r => [r.vf!, r.vt!]))].sort((a, b) => a - b)
    const vs: Version[] = []
    let last = ''
    for (let i = 0; i + 1 < bounds.length; i++) {
      const [lo, hi] = [bounds[i], bounds[i + 1]]
      const live = g.filter(r => r.vf! <= lo && lo < r.vt!)
      if (!live.length) { last = ''; continue }
      const a = newAgg()
      for (const r of live) addAgg(a, r)
      const c = canon(a)
      const prev = vs[vs.length - 1]
      if (prev && prev[1] === lo && c === last) prev[1] = hi
      else vs.push([lo, hi, a])
      last = c
    }
    out.set(k, vs)
  }
  return out
}

/** The version live at scan ordinal `d`, if the path is in that scan. */
export const liveAt = (vs: Version[], d: number): PathAgg | undefined => vs.find(([vf, vt]) => vf <= d && d < vt)?.[2]

// Per isolate and database: one map of rows per index tag, which every fold holds and a fill in this isolate writes
// into. Rows only gain prefixes or advance `hz`, so a refresh merges into it, never replaces it.
type Table = { at: number; rows: Map<string, Hist> }
const tablesByDb = new WeakMap<object, Map<string, Table>>()
const tablesOf = (env: Env): Map<string, Table> => {
  let t = tablesByDb.get(env.DB!)
  if (!t) tablesByDb.set(env.DB!, (t = new Map()))
  return t
}
const refreshing = new Map<string, Promise<Map<string, Hist>>>()
const LOAD_TTL = 300_000
const pricing = new Map<string, Promise<{ groups: number; hz: number } | null>>()
const WAIT = 60_000

/** `tag`'s rows as D1 has them now. */
async function readRows(env: Env, tag: string): Promise<Map<string, Hist>> {
  const { results } = await env.DB!.prepare('SELECT depth, path, hz, versions FROM owner_prefix_hist WHERE idx = ?').bind(tag)
    .all<{ depth: number; path: string; hz: number; versions: string }>()
  return new Map(results.map(r => [key(r.depth, r.path), { hz: r.hz, versions: JSON.parse(r.versions) as Version[] }]))
}

/** `tag`'s rows; `refresh` re-reads D1 (once, shared by concurrent callers) into the same map. */
async function load(env: Env, tag: string, refresh = false): Promise<Map<string, Hist>> {
  const tables = tablesOf(env)
  const cur = tables.get(tag)
  if (cur && !refresh && Date.now() - cur.at < LOAD_TTL) return cur.rows
  return shared(refreshing, tag, async () => {
    const read = await readRows(env, tag)
    const t = tables.get(tag) ?? { at: 0, rows: new Map<string, Hist>() }
    for (const [k, h] of read) if ((t.rows.get(k)?.hz ?? -1) <= h.hz) t.rows.set(k, h)
    t.at = Date.now()
    tables.set(tag, t)
    refreshing.delete(tag)
    return t.rows
  }, WAIT, joinOf(env, 'owner-hist'))
}

/** `want` (path → depth) not priced through scan ordinal `d` (and the roots, if they aren't). */
function staleOf(rows: Map<string, Hist>, want: Map<string, number>, d: number): { keys: Map<string, number>; roots: boolean } {
  const keys = new Map<string, number>()
  for (const [path, depth] of want) if (!((rows.get(key(depth, path))?.hz ?? -1) >= d)) keys.set(path, depth)
  return { keys, roots: !((rows.get(ROOTS)?.hz ?? -1) >= d) }
}

/** Price `keys` (and the roots) over every scan the store holds in one read; write one row per prefix (an
 *  empty `versions`: in none of them) and update the isolate's rows. Shared while in flight only (concurrent fills
 *  of the same prefixes read once): the next scan's fill of them reads again. Null when the store's handle isn't
 *  `tag`'s. */
async function price(env: Env, tag: string, keys: Map<string, number>, roots: boolean): Promise<{ groups: number; hz: number } | null> {
  const id = `${tag}|${roots ? 'R' : ''}|${[...keys].map(([p, d]) => key(d, p)).sort().join('\n')}`
  return inFlight(pricing, id, async () => {
    const asks: Ask[] = [...keys].map(([path, depth]) => ({ depth, path }))
    if (roots) asks.push({ depth: 1, under: '' })
    const fields: (keyof Row)[] = ['path', 'depth', 'usr', 'size', 'n_files', 'cls2', 'cls3', 'cls4']
    const got = await readAsksHistory(env, await ivHeldDates(env), 'path', asks, r => keys.get(r.path) === r.depth || (roots && r.depth === 1),
      { fields, maxGroups: Math.max(400, 2 * asks.length) })
    if (!got || got.index !== tag) return null
    const vs = versionsOf(got.rows)
    const put = new Map<string, Hist>()
    for (const [path, depth] of keys) put.set(key(depth, path), { hz: got.hz, versions: vs.get(key(depth, path)) ?? [] })
    if (roots) {
      for (const [k, v] of vs) if (k.startsWith('1:')) put.set(k, { hz: got.hz, versions: v })
      put.set(ROOTS, { hz: got.hz, versions: [] })
    }
    const now = Math.floor(Date.now() / 1000)
    const stmts = [...put].map(([k, h]) => {
      const i = k.indexOf(':')
      return env.DB!.prepare('INSERT OR REPLACE INTO owner_prefix_hist (idx, depth, path, hz, versions, updated_ts) VALUES (?, ?, ?, ?, ?, ?)')
        .bind(tag, Number(k.slice(0, i)), k.slice(i + 1), h.hz, JSON.stringify(h.versions), now)
    })
    for (let i = 0; i < stmts.length; i += 100) await env.DB!.batch(stmts.slice(i, i + 100))
    const held = tablesOf(env).get(tag)?.rows
    if (held) for (const [k, h] of put) if ((held.get(k)?.hz ?? -1) <= h.hz) held.set(k, h)
    return { groups: got.groups, hz: got.hz }
  }, joinOf(env, 'owner-hist-price'))
}

/** What a fold found unpriced through its scan: the prefixes (index paths, sorted) and whether the roots are. */
export interface Stale { paths: string[]; roots: boolean }

/** How long a read of `tag`'s rows stands when a fold finds something unpriced: a series' folds (one per scan)
 *  then share one re-read, not one each. */
const STALE_REREAD = 2_000

/** `date`'s aggregates (scan ordinal `d`) for `want` and the roots from the history rows at `tag`: one D1 read per
 *  generation, held by the isolate, and one more (at most every `STALE_REREAD`) when something isn't priced through
 *  `d`: another isolate's fill may have landed since. `stale`: what still isn't. Nothing is priced here. */
export async function histAggs(env: Env, tag: string, d: number, want: Map<string, number>): Promise<{ aggs: Map<string, PathAgg>; buckets: string[] } | { stale: Stale }> {
  let fresh = await load(env, tag)
  let { keys, roots } = staleOf(fresh, want, d)
  if ((keys.size || roots) && Date.now() - (tablesOf(env).get(tag)?.at ?? 0) >= STALE_REREAD) {
    fresh = await load(env, tag, true)
    ;({ keys, roots } = staleOf(fresh, want, d))
  }
  if (keys.size || roots) return { stale: { paths: [...keys.keys()].sort(), roots } }
  const aggs = new Map<string, PathAgg>()
  const buckets: string[] = []
  for (const [k, h] of fresh) {
    if (!k.startsWith('1:')) continue
    const a = liveAt(h.versions, d)
    if (a) { buckets.push(k.slice(2)); aggs.set(k.slice(2), a) }
  }
  for (const [path, depth] of want) {
    if (depth === 1) continue
    const a = liveAt(fresh.get(key(depth, path))!.versions, d)
    if (a) aggs.set(path, a)
  }
  return { aggs, buckets: buckets.sort() }
}

/** What of `want` (and the roots) `tag`'s rows in D1 don't price through scan ordinal `hz`. */
export async function histStale(env: Env, tag: string, want: Map<string, number>, hz: number): Promise<{ remaining: number; roots: boolean }> {
  const { keys, roots } = staleOf(await readRows(env, tag), want, hz)
  return { remaining: keys.size, roots }
}

/** The fill: price up to `n` of `want` not priced through the store's newest scan (and the roots, when they
 *  aren't), in one read. `remaining` counts what is still unpriced after it, `hz` is the scan ordinal priced
 *  through. Idempotent and resumable: each call prices what D1's rows lack when it runs (D1's, not the isolate's
 *  copy: a row deleted there is priced again). Null: the store holds no scan, or its handle isn't `tag`'s. */
export async function fillHist(env: Env, tag: string, want: Map<string, number>, n: number): Promise<{ priced: number; remaining: number; groups: number; hz: number } | null> {
  const hz = await ivHorizon(env)
  if (hz == null) return null
  const { keys, roots } = staleOf(await readRows(env, tag), want, hz)
  const batch = new Map([...keys].slice(0, n))
  if (!batch.size && !roots) return { priced: 0, remaining: 0, groups: 0, hz }
  const got = await price(env, tag, batch, roots)
  if (!got) return null
  return { priced: batch.size, remaining: keys.size - batch.size, groups: got.groups, hz: got.hz }
}

/** Rows one carry or prune statement touches at most (D1 fails a statement over too many rows). */
export const HIST_PAGE = 2000

/** `from`'s rows copied under `to`: a compaction's new generation holds the same scans, so each prefix's versions
 *  over scan ordinals are the same. A row `to` already prices further is kept. Returns the rows `from` holds. */
export async function carryHist(env: Env, from: string, to: string): Promise<number> {
  const n = (await env.DB!.prepare('SELECT COUNT(*) AS n FROM owner_prefix_hist WHERE idx = ?').bind(from).first<{ n: number }>())?.n ?? 0
  const now = Math.floor(Date.now() / 1000)
  for (let off = 0; from !== to && off < n; off += HIST_PAGE) {
    await env.DB!.prepare(
      'INSERT INTO owner_prefix_hist (idx, depth, path, hz, versions, updated_ts) ' +
      'SELECT ?, depth, path, hz, versions, ? FROM (SELECT depth, path, hz, versions FROM owner_prefix_hist WHERE idx = ? ORDER BY depth, path LIMIT ? OFFSET ?) WHERE true ' +
      'ON CONFLICT (idx, depth, path) DO UPDATE SET hz = excluded.hz, versions = excluded.versions, updated_ts = excluded.updated_ts WHERE excluded.hz >= owner_prefix_hist.hz',
    ).bind(to, now, from, HIST_PAGE, off).run()
  }
  tablesOf(env).delete(to)
  return n
}

/** Every index tag with rows, and how many. */
export async function histTags(env: Env): Promise<{ idx: string; rows: number }[]> {
  return (await env.DB!.prepare('SELECT idx, COUNT(*) AS rows FROM owner_prefix_hist GROUP BY idx ORDER BY idx').all<{ idx: string; rows: number }>()).results
}

/** `tag`'s rows deleted, `HIST_PAGE` a statement. Returns how many. */
export async function dropHist(env: Env, tag: string): Promise<number> {
  let n = 0
  for (;;) {
    const r = await env.DB!.prepare('DELETE FROM owner_prefix_hist WHERE rowid IN (SELECT rowid FROM owner_prefix_hist WHERE idx = ? LIMIT ?)').bind(tag, HIST_PAGE).run()
    const changes = Number(r.meta.changes ?? 0)
    n += changes
    if (changes < HIST_PAGE) break
  }
  tablesOf(env).delete(tag)
  return n
}
