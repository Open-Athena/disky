/** Card data for the non-map pages (specs/done/dogi.md, phase 2): `/staged`,
 * `/users`, `/user/:id` (the map card under that user's lens) and
 * `/assignments`. Same rule as the map: the data read is the page's; the tier
 * only decides what is labelled. Failures become a card that says so. */
import type { Env } from '../auth.js'
import { canonId, loadRegistry } from '../identity.js'
import { loadLedger } from '../ledger.js'
import { ownerTotals } from '../ownerTotals.js'
import { openPlanId } from '../plans.js'
import { type PrefixStat } from '../prefixes.js'
import { countedItems, deletedItems, stagedStats, threadBands } from '../stagedSlack.js'
import { HI_CONTRAST } from '../../../src/colors.js'
import { filterStaged } from '../../../src/stagedFilter.js'
import { ownerIndex } from '../../../src/ownerIndex.js'
import { type CardData, type CardTile, type Grid, fmtB } from './card.js'
import { mapCard, ownerColors, resolveScan, UNOWNED_COLOR, type Site } from './data.js'
import type { OgTier } from './sign.js'

const fmtN = (n: number) => n.toLocaleString('en-US')
type Reg = Awaited<ReturnType<typeof loadRegistry>>

async function regOf(env: Env): Promise<Reg> {
  return loadRegistry(env).catch(() => ({}) as Reg)
}

async function scanUsersOf(env: Env, date: string) {
  // Colour slots follow the scan's ranking (the map's), from owner totals.
  const t = await ownerTotals(env, date)
  return Object.entries(t.users).map(([u, v]) => ({ u, b: v.b })).sort((a, b) => b.b - a.b)
}

/** `/user/<id>`: the map at the root, under that user's lens. */
export function userCard(env: Env, site: Site, title: string, params: Record<string, string>, tier: OgTier): Promise<CardData> {
  return mapCard(env, site, title, { o: params.id, ...(params.d ? { d: params.d } : {}) }, tier)
}

/** `/users`: everyone's owned bytes as a treemap, one tile per person. */
export async function usersCard(env: Env, site: Site, title: string, params: Record<string, string>, tier: OgTier): Promise<CardData> {
  const base = { tier, site: site.name, title, tiles: [] as CardTile[] }
  const date = await resolveScan(env, params.d)
  if (!date) return { ...base, subtitle: '', total: '', empty: 'no scans yet' }
  const [t, reg] = await Promise.all([ownerTotals(env, date), regOf(env)])
  const users = Object.entries(t.users).map(([u, v]) => ({ u, b: v.b })).sort((a, b) => b.b - a.b)
  const color = ownerColors(users)
  const owned = users.reduce((s, u) => s + u.b, 0)
  const tiles: CardTile[] = users.map(u => ({ name: reg[u.u]?.name ?? u.u, b: u.b, color: color(u.u) }))
  if (t.bytes > owned) tiles.push({ name: 'unowned', b: t.bytes - owned, color: UNOWNED_COLOR })
  return {
    ...base,
    subtitle: `scan ${date} · ${users.length} people`,
    total: `${fmtB(owned)} owned of ${fmtB(t.bytes)}`,
    tiles,
  }
}

/** `/assignments`: the assigner × assignee matrix as a heatmap. */
export async function assignmentsCard(env: Env, site: Site, title: string, params: Record<string, string>, tier: OgTier): Promise<CardData> {
  const base = { tier, site: site.name, title, tiles: [] as CardTile[] }
  const date = await resolveScan(env, params.d)
  if (!date || !env.DB) return { ...base, subtitle: '', total: '', empty: 'no scans yet' }
  const [t, reg, ue] = await Promise.all([
    ownerTotals(env, date),
    regOf(env),
    env.DB.prepare('SELECT email, user FROM user_emails').all<{ email: string; user: string }>().then(r => r.results).catch(() => []),
  ])
  const emap = new Map(ue.map(r => [r.email.toLowerCase(), r.user]))
  const cells = new Map<string, number>()
  for (const c of t.assignments) {
    if (c.repainted_by || !c.owner) continue
    const by = c.who ? (emap.get(c.who.toLowerCase()) ?? c.who) : 'unknown'
    const k = `${by}\u0000${c.owner}`
    cells.set(k, (cells.get(k) ?? 0) + c.bytes)
  }
  const rowB = new Map<string, number>()
  const colB = new Map<string, number>()
  for (const [k, b] of cells) {
    const [by, to] = k.split('\u0000')
    rowB.set(by, (rowB.get(by) ?? 0) + b)
    colB.set(to, (colB.get(to) ?? 0) + b)
  }
  const rows = [...rowB].sort((a, b) => b[1] - a[1]).map(e => e[0]).slice(0, 9)
  const cols = [...colB].sort((a, b) => b[1] - a[1]).map(e => e[0]).slice(0, 11)
  const name = (u: string) => reg[canonId(u, reg)]?.name ?? u.replace(/@.*$/, '')
  const grid: Grid = {
    rows: rows.map(name),
    cols: cols.map(name),
    cells: [...cells].flatMap(([k, b]) => {
      const [by, to] = k.split('\u0000')
      const i = rows.indexOf(by)
      const j = cols.indexOf(to)
      return i >= 0 && j >= 0 ? [[i, j, b] as [number, number, number]] : []
    }),
  }
  const total = [...cells.values()].reduce((s, b) => s + b, 0)
  return {
    ...base,
    subtitle: `scan ${date} · ${rowB.size} assigners × ${colB.size} assignees`,
    total: `${fmtB(total)} assigned`,
    grid,
    ...(cells.size ? {} : { empty: 'no assignments yet' }),
  }
}

/** The leading dirs (`a/b/`, or `''`) every path in `paths` sits under, never a path's own last segment: each keeps a
 * name after it's cut. */
function sharedDirs(paths: string[]): string {
  const dirs = paths.map(p => p.replace(/\/$/, '').split('/').slice(0, -1))
  let n = Math.min(...dirs.map(d => d.length))
  for (let i = 0; i < n; i++) {
    if (dirs.some(d => d[i] !== dirs[0][i])) { n = i; break }
  }
  return dirs.length && n > 0 ? `${dirs[0].slice(0, n).join('/')}/` : ''
}

/** One staged item: its stats (at the scan; a deleted one's, what its runs deleted), effective owner, and whether a real run
 *  of the plan's thread deleted it. */
interface Item { prefix: string; addedBy: string; stat?: PrefixStat; owner: string | null; deleted?: boolean }

/** A deleted prefix's colour, and every still-staged one's once the thread has deleted some (the card then shows state). */
export const DELETED_COLOR = '#8e3b36'
export const STAGED_COLOR = '#c9922e'

/** `/staged` (+ `q`): what the open plan's thread staged, a tile per bucket holding its items. Before any real run deleted
 * in the thread: the items still staged at the latest scan, coloured by owner (the ledger's assignee, else the scan's top
 * owner). Once one has: every item the thread staged, deleted ones sized by what was deleted, coloured by state (deleted /
 * still staged), so an emptied queue still shows what it held. */
export async function stagedCard(env: Env, site: Site, title: string, params: Record<string, string>, tier: OgTier): Promise<CardData> {
  const base = { tier, site: site.name, title, tiles: [] as CardTile[] }
  if (!env.DB) return { ...base, subtitle: '', total: '', empty: 'nothing staged' }
  const plan = await openPlanId(env.DB).catch(() => null)
  if (plan == null) return { ...base, subtitle: '', total: '', empty: 'nothing staged' }
  // The thread: a closed one's window (`th`: `<since>-<until>`, its parent's post to the next thread's), else the plan's
  // current thread (since its parent's post). Its items: those staged before it ended.
  const th = /^(\d+)-(\d+)$/.exec(params.th ?? '')
  const thread = th ? null : await env.DB.prepare('SELECT slack_ts FROM plans WHERE id = ?').bind(plan).first<{ slack_ts: string | null }>().catch(() => null)
  const since = th ? Number(th[1]) : thread?.slack_ts ? Math.floor(Number(thread.slack_ts)) : null
  const until = th ? Number(th[2]) : null
  const rows = (await env.DB.prepare("SELECT prefix, COALESCE(kind, 'prefix') AS kind, added_by FROM plan_items WHERE plan_id = ? AND (? IS NULL OR added_ts < ?)").bind(plan, until, until).all<{ prefix: string; kind: 'prefix' | 'object'; added_by: string }>()).results
  // What the thread's real runs deleted (a real run unstages what it deleted, so these are no longer plan items).
  const bands: Record<string, PrefixStat> = since == null ? {} : await threadBands(env.DB, plan, since, until).catch(() => ({}))
  // Every prefix still staged, sized at the latest scan (as the Slack parent's totals are).
  const at = rows.length ? await stagedStats(env, rows.map(r => r.prefix), env.DB) : null
  if (!Object.keys(bands).length && (!rows.length || !at)) return { ...base, subtitle: `plan #${plan}`, total: '', empty: 'nothing staged' }
  const date = at?.scan ?? null
  const stats = at?.stats ?? {}
  const [reg, ledger, users, deleted] = await Promise.all([
    regOf(env),
    loadLedger(env).catch(() => ({ ownerRows: [] })),
    date ? scanUsersOf(env, date).catch(() => []) : Promise.resolve([]),
    deletedItems(env.DB, plan),
  ])
  const idx = ownerIndex({ owners: ledger.ownerRows.map(r => ({ ...r, who: r.who ?? '', memo: null })) })
  // The prefixes the Slack parent counts: neither deleted by a real run nor empty at the scan.
  const ownerOf = (r: { prefix: string; kind: 'prefix' | 'object' }, stat?: PrefixStat): string | null => {
    const assignment = idx.assignmentOf(r.prefix, r.kind)
    return assignment ? canonId(assignment.who, reg) : stat?.owners?.[0]?.[0] ?? null
  }
  const byPrefix = new Map(rows.map(r => [r.prefix, r]))
  let items: Item[] = [
    // what the thread's real runs deleted, sized by what they deleted
    ...Object.entries(bands).map(([prefix, stat]) => {
      const r = byPrefix.get(prefix) ?? { prefix, kind: 'prefix' as const, added_by: '' }
      return { prefix, addedBy: r.added_by, stat, owner: ownerOf(r), deleted: true }
    }),
    ...countedItems(rows, deleted, at ? stats : null).filter(r => !bands[r.prefix] && stats[r.prefix]).map(r => ({ prefix: r.prefix, addedBy: r.added_by, stat: stats[r.prefix], owner: ownerOf(r, stats[r.prefix]) })),
  ]
  const name = (who: string) => reg[canonId(who, reg)]?.name ?? who.replace(/@.*$/, '')
  if (params.q) {
    const f = filterStaged(items, params.q, i => ({ prefix: i.prefix, owners: i.owner ? [i.owner] : [], stagedBy: i.addedBy }), name)
    if (f.error) return { ...base, subtitle: `plan #${plan}${date ? ` · scan ${date}` : ''}`, total: '', empty: 'invalid filter' }
    items = f.rows
  }
  const owners = ownerColors(users)
  // Once the thread deleted some, the colours say state; before, owner (or, with no owner known, a store without
  // attribution like cw's, each item's rank, as the map's tree colours do: else every item is its bucket's grey).
  const byState = items.some(i => i.deleted)
  const anyOwner = !byState && items.some(i => i.owner != null)
  const rank = new Map([...items].sort((x, y) => (y.stat?.bytes ?? 0) - (x.stat?.bytes ?? 0)).map((it, i) => [it.prefix, i]))
  const color = (it: Item) => byState ? (it.deleted ? DELETED_COLOR : STAGED_COLOR)
    : anyOwner ? owners(it.owner) : HI_CONTRAST[(rank.get(it.prefix) ?? 0) % HI_CONTRAST.length]
  const byBucket = new Map<string, Item[]>()
  for (const it of items) {
    const bucket = it.prefix.replace(/^[a-z0-9]+:\/\//, '').split('/')[0]
    byBucket.set(bucket, [...(byBucket.get(bucket) ?? []), it])
  }
  const tiles: CardTile[] = [...byBucket].map(([bucket, its]) => {
    // each item named within its bucket, less the dirs every item shares, which the tile names after the bucket
    const rel = its.map(i => i.prefix.replace(/^[a-z0-9]+:\/\//, '').slice(bucket.length).replace(/^\//, ''))
    const shared = sharedDirs(rel)
    const kids = its.map((i, k) => ({ name: rel[k].slice(shared.length), b: i.stat?.bytes ?? 0, color: color(i) }))
    const b = kids.reduce((s, k) => s + k.b, 0)
    const top = [...its].sort((x, y) => (y.stat?.bytes ?? 0) - (x.stat?.bytes ?? 0))[0]
    const tile = byState ? (its.every(i => i.deleted) ? DELETED_COLOR : its.every(i => !i.deleted) ? STAGED_COLOR : UNOWNED_COLOR)
      : anyOwner ? owners(top?.owner ?? null) : UNOWNED_COLOR
    return { name: shared ? `${bucket}/${shared.replace(/\/$/, '')}` : bucket, b, color: tile, kids }
  })
  const sum = (its: Item[], k: 'bytes' | 'objects') => its.reduce((s, i) => s + (i.stat?.[k] ?? 0), 0)
  const b = sum(items, 'bytes')
  const o = sum(items, 'objects')
  const gone = items.filter(i => i.deleted)
  const goneB = sum(gone, 'bytes')
  const byOwner = new Map<string | null, number>()
  for (const i of items) byOwner.set(i.owner, (byOwner.get(i.owner) ?? 0) + (i.stat?.bytes ?? 0))
  const legend = byState ? [{ label: 'deleted', color: DELETED_COLOR, b: goneB }, { label: 'still staged', color: STAGED_COLOR, b: b - goneB }].filter(l => l.b > 0)
    : anyOwner ? [...byOwner].filter(([, v]) => v > 0).sort((x, y) => y[1] - x[1]).slice(0, 6)
      .map(([u, v]) => ({ label: u ? (reg[u]?.name ?? u) : 'unowned', color: owners(u), b: v })) : []
  const state = !byState ? '' : gone.length === items.length ? ' · all deleted' : ` · ${fmtB(goneB)} deleted, ${fmtB(b - goneB)} left`
  return {
    ...base,
    subtitle: `plan #${plan}${date ? ` · scan ${date}` : ''}${params.q ? ` · filter “${params.q}”` : ''}`,
    total: `${fmtN(items.length)} prefixes · ${fmtB(b)} · ${fmtN(o)} objects${state}`,
    tiles,
    legend,
    ...(b ? {} : { empty: params.q ? 'no matches' : 'nothing left at this scan' }),
  }
}
