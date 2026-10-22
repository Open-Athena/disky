/**
 * The activity feed's pure parts (specs/done/activity-feed.md): event kinds and their defaults, which requests and
 * session-log events are which kind, bursts, and the Slack / Discord renderings. No Workers or DOM types; the D1 and
 * sink I/O is `activity.ts`.
 */
import type { SlogEvent } from './sessionLogShape.js'

export const NOTIFY = ['off', 'count', 'line'] as const
export type Notify = typeof NOTIFY[number]

export interface KindDef {
  kind: string
  source: 'server' | 'client'
  /** What the kind covers (the `/admin` table). */
  covers: string
  /** Counted noun: one, many. */
  noun: [string, string]
  log: boolean
  notify: Notify
}

const def = (kind: string, source: KindDef['source'], covers: string, noun: [string, string], notify: Notify, log = true): KindDef =>
  ({ kind, source, covers, noun, log, notify })

/** Every kind, in report order, with its defaults. */
export const KIND_DEFS: KindDef[] = [
  def('assign', 'server', 'Owner assigned or cleared (a folder or an object)', ['assignment', 'assignments'], 'line'),
  def('note', 'server', 'Chart note added, edited or removed', ['note', 'notes'], 'line'),
  def('stage', 'server', 'Staged-deletion edits (each plan has its own thread)', ['staging edit', 'staging edits'], 'count'),
  def('deletion', 'server', 'Deletion runs dispatched, stopped, undone or purged', ['deletion action', 'deletion actions'], 'line'),
  def('access', 'server', 'Grants, share links, access requests, the sign-in allowlist', ['access change', 'access changes'], 'line'),
  def('admin', 'server', 'Admin console edits and site settings', ['admin edit', 'admin edits'], 'line'),
  def('token', 'server', 'Agent / CLI token minted', ['token', 'tokens'], 'line'),
  def('profile', 'server', 'Profile name or avatar', ['profile edit', 'profile edits'], 'count'),
  def('write', 'server', 'Any other change', ['other change', 'other changes'], 'count'),
  def('view', 'client', 'Page loads', ['page load', 'page loads'], 'count'),
  def('nav', 'client', 'URL changes: routes, drills, query params, the section in view', ['URL change', 'URL changes'], 'count'),
  def('click', 'client', 'Clicks on controls (buttons, links, menus, checkboxes)', ['click', 'clicks'], 'count'),
  def('filter', 'client', 'Path-filter input', ['filter', 'filters'], 'count'),
  def('pick', 'client', 'Picker choices (selects, the assign and scan pickers)', ['pick', 'picks'], 'count'),
  def('error', 'client', 'Errors: uncaught, rejected, console, failed requests', ['error', 'errors'], 'count'),
  def('api', 'client', 'Other requests the page made', ['request', 'requests'], 'off'),
  def('prefetch', 'client', 'Hover prefetches', ['prefetch', 'prefetches'], 'off'),
  def('viewport', 'client', 'Window size', ['resize', 'resizes'], 'off'),
  def('vis', 'client', 'Tab shown / hidden', ['visibility change', 'visibility changes'], 'off'),
]
export const KIND_BY_NAME: Record<string, KindDef> = Object.fromEntries(KIND_DEFS.map(d => [d.kind, d]))

export interface KindConf { log: boolean; notify: Notify }
export type KindsConf = Record<string, KindConf>
export interface KindRow { kind: string; log: number; notify: string }

/** The defaults, overridden by the stored rows (rows for unknown kinds, or bad `notify` values, are ignored). */
export function mergeKinds(rows: readonly KindRow[]): KindsConf {
  const out: KindsConf = Object.fromEntries(KIND_DEFS.map(d => [d.kind, { log: d.log, notify: d.notify }]))
  for (const r of rows) {
    if (!out[r.kind] || !(NOTIFY as readonly string[]).includes(r.notify)) continue
    out[r.kind] = { log: !!r.log, notify: r.notify as Notify }
  }
  return out
}

/** Writes that are reads or plumbing, never events. */
const NOT_EVENTS = [
  /^\/api\/session-log(\/sessions.*)?$/, /^\/api\/footer-cache$/, /^\/api\/digest-image$/, /^\/api\/static-bench$/,
  /^\/api\/prefixes$/, /^\/auth\//, /^\/api\/auth\/(exchange|logout|track|request|email)(\/|$)/,
  /^\/slack\//,
]

/** A successful write's kind, or null when it isn't an event. */
export function serverKind(method: string, path: string): string | null {
  if (method === 'GET' || method === 'HEAD' || method === 'OPTIONS') return null
  if (NOT_EVENTS.some(re => re.test(path))) return null
  if (path === '/api/actions') return 'assign'
  if (path === '/api/annotations') return 'note'
  if (path.startsWith('/api/plans')) return 'stage'
  if (/^\/api\/(plan-)?sweep\//.test(path)) return 'deletion'
  if (path === '/api/token') return 'token'
  if (path === '/api/auth/profile' || path === '/api/auth/avatar') return 'profile'
  if (path.startsWith('/api/auth/') || path.startsWith('/api/og/')) return 'access'
  if (path.startsWith('/api/db') || path === '/api/session-log/switch' || path.startsWith('/api/activity/')) return 'admin'
  return 'write'
}

/** A session-log event's kind, or null (`dropped` markers). */
export function clientKind(e: Pick<SlogEvent, 'k'> & { s?: unknown }): string | null {
  switch (e.k) {
    case 'start': return 'view'
    case 'error': case 'reject': case 'console': return 'error'
    case 'api': return typeof e.s === 'number' && (e.s === 0 || e.s >= 500) ? 'error' : 'api'
    case 'dropped': return null
    default: return e.k
  }
}

/** The auth package's admin writes, as what they did. */
export function authLine(method: string, path: string): string {
  const seg = path.replace(/^\/api\/auth\/?/, '').split('/').filter(Boolean)
  const [what, id, verb] = seg
  if (what === 'grants') {
    if (!id) return 'minted a grant'
    if (verb) return `${verb === 'disable' ? 'disabled' : verb === 'enable' ? 'enabled' : verb === 'revoke' ? 'revoked' : verb === 'rotate' ? 'rotated' : verb} grant ${id}`
    return method === 'PATCH' ? `edited grant ${id}` : `${method} grant ${id}`
  }
  if (what === 'requests' && id && (verb === 'approve' || verb === 'deny')) return `${verb === 'approve' ? 'approved' : 'denied'} access request ${id}`
  if (what === 'decide') return 'decided an access request (emailed link)'
  if (what === 'allowed') {
    if (id === 'sync') return 'synced the sign-in allowlist'
    if (method === 'DELETE' && id) return `removed ${decodeURIComponent(id)} from the sign-in allowlist`
    return 'added to the sign-in allowlist'
  }
  if (what === 'profile' || what === 'avatar') return what === 'avatar' ? 'changed their avatar' : 'edited their profile'
  return `${method} ${path}`
}

/** A route's line when it set none of its own. */
export function genericLine(method: string, path: string): string {
  return path.startsWith('/api/auth/') ? authLine(method, path) : `${method} ${path}`
}

/** One server event, as recorded. */
export interface ServerEvent {
  ts: number
  kind: string
  actor: string
  via: string
  method: string
  route: string
  line: string
  detail?: Record<string, unknown>
}

/** A route's account of one thing a request did: its line and any structured fields (`bytes`, `owner`, …). */
export interface Detail {
  line: string
  kind?: string
  [k: string]: unknown
}

/** One actor's activity: no gap over `gapS` between events. */
export interface Burst {
  id: number
  actor: string
  via: string
  origin: string
  first_ts: number
  last_ts: number
  counts: Record<string, number>
  /** Paths viewed (`view` / `nav`), by count. */
  pages: Record<string, number>
  sids: string[]
}

/** Whether an event at `ts` joins the actor's latest burst (else it starts a new one). */
export const joins = (latest: Pick<Burst, 'last_ts'> | null, ts: number, gapS: number): boolean =>
  !!latest && ts - latest.last_ts <= gapS && ts >= latest.last_ts - gapS

/** `pages` with `more` added, kept to the `keep` most-viewed (ties by path). */
export function addPages(pages: Record<string, number>, more: Record<string, number>, keep = 20): Record<string, number> {
  const all = { ...pages }
  for (const [p, n] of Object.entries(more)) all[p] = (all[p] ?? 0) + n
  return Object.fromEntries(Object.entries(all).sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1)).slice(0, keep))
}

/** A batch's client events, per kind (the recorded kinds only), and the paths viewed. */
export function tallyClient(events: readonly SlogEvent[], conf: KindsConf): { counts: Record<string, number>; pages: Record<string, number> } {
  const counts: Record<string, number> = {}
  const pages: Record<string, number> = {}
  for (const e of events) {
    const k = clientKind(e)
    if (!k || !conf[k]?.log) continue
    counts[k] = (counts[k] ?? 0) + 1
    if ((k === 'view' || k === 'nav') && typeof e.u === 'string') {
      const p = pathOf(e.u)
      pages[p] = (pages[p] ?? 0) + 1
    }
  }
  return { counts, pages }
}

/** A logged URL's path (no query or hash). */
export const pathOf = (u: string): string => {
  const p = u.replace(/^[a-z]+:\/\/[^/]+/i, '').replace(/[?#].*$/, '')
  return p || '/'
}

/** The events a batch keeps: the recorded kinds (`dropped` markers always). */
export const keptEvents = (events: readonly SlogEvent[], conf: KindsConf): SlogEvent[] =>
  events.filter(e => { const k = clientKind(e); return k === null || !!conf[k]?.log })

/** Whether any of a burst's counted kinds is reported. */
export const reported = (counts: Record<string, number>, conf: KindsConf): boolean =>
  Object.entries(counts).some(([k, n]) => n > 0 && (conf[k]?.notify ?? 'off') !== 'off')

// ---- rendering ----

export interface Fmt {
  bold: (s: string) => string
  link: (url: string, text: string) => string
  code: (s: string) => string
}
export const SLACK: Fmt = {
  bold: s => `*${s}*`,
  link: (url, text) => `<${url}|${text.replace(/[<>|]/g, '')}>`,
  code: s => `\`${s.replace(/`/g, "'")}\``,
}
export const DISCORD: Fmt = {
  bold: s => `**${s}**`,
  link: (url, text) => `[${text.replace(/[[\]]/g, '')}](<${url}>)`,
  code: s => `\`${s.replace(/`/g, "'")}\``,
}

const fmtN = (n: number): string => n.toLocaleString('en-US')
const hm = (ts: number): string => new Date(ts * 1000).toISOString().slice(11, 16)
const day = (ts: number): string => new Date(ts * 1000).toISOString().slice(0, 10)

/** IEC, as the staged-deletion threads in the same channel print sizes. */
export const fmtBytes = (n: number): string => {
  const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${i ? v.toFixed(1) : v} ${u[i]}`
}

/** How a burst names its actor: the email's local part, else the grant's name as given. */
export const actorName = (actor: string): string => actor.includes('@') ? actor.replace(/@.*$/, '') : actor

/** `n noun(s)` for a kind. */
export const countText = (kind: string, n: number): string => {
  const d = KIND_BY_NAME[kind]
  return `${fmtN(n)} ${d ? (n === 1 ? d.noun[0] : d.noun[1]) : kind}`
}

/** What the summary shows besides counts, from the burst's server events. */
export interface Highlights {
  /** The assignment with the most bytes. */
  largest?: { pattern: string; bytes: number; owner: string | null }
  /** Owners assigned to (null = cleared), in first-seen order. */
  owners?: (string | null)[]
  /** The staged plans touched. */
  plans?: number[]
}

export function highlights(events: readonly Pick<ServerEvent, 'kind' | 'detail'>[]): Highlights {
  const out: Highlights = {}
  const owners: (string | null)[] = []
  const plans: number[] = []
  for (const e of events) {
    const d = e.detail ?? {}
    if (e.kind === 'assign') {
      const owner = typeof d.owner === 'string' ? d.owner : null
      if (!owners.includes(owner)) owners.push(owner)
      if (typeof d.bytes === 'number' && typeof d.pattern === 'string' && (!out.largest || d.bytes > out.largest.bytes)) {
        out.largest = { pattern: d.pattern, bytes: d.bytes, owner }
      }
    }
    if (e.kind === 'stage' && typeof d.plan === 'number' && !plans.includes(d.plan)) plans.push(d.plan)
  }
  if (owners.length) out.owners = owners
  if (plans.length) out.plans = plans
  return out
}

export interface SummaryOpts {
  fmt: Fmt
  conf: KindsConf
  highlights: Highlights
  /** `/admin/activity?burst=<id>` etc. are built from the burst's origin. */
  topPages?: number
}

const via = (v: string): string => v === 'grant' ? 'agent token / share link' : v === 'public' ? 'signed out' : 'browser'

/** The burst's summary: who, how, when, the reported counts, highlights, links. */
export function renderSummary(b: Burst, o: SummaryOpts): string {
  const { fmt, conf } = o
  const span = b.first_ts === b.last_ts ? hm(b.first_ts) : `${hm(b.first_ts)}–${hm(b.last_ts)}`
  const lines = [`${fmt.bold(actorName(b.actor))} (${via(b.via)}) · ${day(b.first_ts)} ${span}Z`]
  const counts = KIND_DEFS.filter(d => (b.counts[d.kind] ?? 0) > 0 && conf[d.kind]?.notify !== 'off').map(d => countText(d.kind, b.counts[d.kind]))
  if (counts.length) lines.push(counts.join(' · '))
  const h = o.highlights
  if (h.largest) lines.push(`Largest assignment: ${fmt.code(h.largest.pattern)} (${fmtBytes(h.largest.bytes)}) → ${h.largest.owner ?? 'nobody'}`)
  if (h.owners && (h.owners.length > 1 || !h.largest)) lines.push(`Owners: ${h.owners.map(x => x ?? 'cleared').join(', ')}`)
  const top = Object.entries(b.pages).slice(0, o.topPages ?? 3)
  if (top.length && conf.view?.notify !== 'off') lines.push(`Pages: ${top.map(([p, n]) => `${fmt.code(p)}${n > 1 ? ` ×${n}` : ''}`).join(', ')}`)
  const links = [fmt.link(`${b.origin}/admin/activity?burst=${b.id}`, 'activity')]
  if (b.sids.length) links.push(fmt.link(`${b.origin}/admin/sessions?u=${encodeURIComponent(b.actor)}`, b.sids.length === 1 ? 'session' : `${b.sids.length} sessions`))
  for (const p of h.plans ?? []) links.push(fmt.link(`${b.origin}/staged?plan=${p}`, `plan ${p}`))
  lines.push(links.join(' · '))
  return lines.join('\n')
}

/** Lines one reply (or Discord section) lists, before `+N more`. */
export const MAX_LINES = 30

/** A `line` kind's list: a header, then one line per event (oldest first), cut at `max`. */
export function renderLines(kind: string, events: readonly Pick<ServerEvent, 'ts' | 'line'>[], fmt: Fmt, max = MAX_LINES): string {
  const shown = events.slice(0, max)
  const out = [fmt.bold(countText(kind, events.length)), ...shown.map(e => `• ${hm(e.ts)} ${e.line}`)]
  if (events.length > max) out.push(`+${fmtN(events.length - max)} more`)
  return out.join('\n')
}

/** Discord's message limit. */
export const DISCORD_MAX = 2000

/** One Discord message: the summary, then each `line` kind's list, cut to fit (`+N more` and the activity link
 *  stay). */
export function renderDiscord(summary: string, sections: readonly string[], activityUrl: string, max = DISCORD_MAX): string {
  let out = summary
  const tail = `\n…more in [activity](<${activityUrl}>)`
  for (const s of sections) {
    const next = `${out}\n\n${s}`
    if (next.length > max) {
      const room = max - out.length - tail.length - 2
      const cut = room > 40 ? `\n\n${s.slice(0, s.lastIndexOf('\n', room) > 0 ? s.lastIndexOf('\n', room) : room)}` : ''
      return (out + cut + tail).slice(0, max)
    }
    out = next
  }
  return out
}
