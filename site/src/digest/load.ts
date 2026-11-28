/** A month's digest rows from its scans' `meta.json`s (`digest.load_window` / `window_scans` / `scans_as_of`,
 * `digest_quota.load_month` / `rows_from_meta` / `scan_triggers`, ported; goldens: `./load.test.ts` over
 * `cloud/tests/fixtures/digest/load/*.json`, pinned by `cloud/tests/test_digest_load_parity.py`). Pure: the caller lists
 * the scans and reads the metas (`functions/_lib/digestLoad.ts`), so a Worker converges a thread from what the job
 * would have (specs/slack-in-workers.md). DOM-free. */
import { minSlug, SCAN_ID_RE, startKey, timesNeeded } from '../scanSlug'
import { monthOf, weekOf, windowStart } from './common'
import { dayOf, pyRound } from './pyfmt'
import type { QuotaRows, QuotaScan } from './quota'

/** The OP window's reach before the latest scan (`digest.WINDOW_DAYS`). */
export const WINDOW_DAYS = 30

/** What `rows_from_meta` reads of a scan's `meta.json`. */
export interface BucketMeta { total_bytes: number; total_objects: number; class_bytes?: Record<string, number> }
export interface ScanMeta {
  total_bytes: number
  total_objects: number
  buckets?: Record<string, BucketMeta>
  class_bytes?: Record<string, number>
  started?: string
}

/** `scan_id.META_PATH`: a published scan's `<id>/meta.json`. */
const META_PATH = new RegExp(`/(${SCAN_ID_RE.source.slice(1, -1)})/meta\\.json$`)

/** The scan ids among ``keys`` (a listing under the snapshots root), sorted: ids sort chronologically (`list_scans`). */
export function scanIds(keys: Iterable<string>): string[] {
  const out: string[] = []
  for (const k of keys) {
    const m = META_PATH.exec(`/${k}`)
    if (m) out.push(m[1])
  }
  return out.sort()
}

/** ``scans`` (sorted) as they stood at the end of ``asOf`` (`YYYY-MM-DD`, UTC); all when null (`scans_as_of`). */
export const scansAsOf = (scans: string[], asOf: string | null): string[] => (asOf === null ? scans : scans.filter(s => s.slice(0, 10) <= asOf))

/** ``month``'s (`YYYY-MM`) own scans, and ``lead``: every scan of the last day before it (`load_window`, before its
 *  reads). Null: the month has no scans. */
export function windowIds(scans: string[], month: string): { lead: string[]; inMonth: string[] } | null {
  const inMonth = scans.filter(s => s.startsWith(`${month}-`))
  if (!inMonth.length) return null
  const before = scans.filter(s => s < inMonth[0])
  const last = before.at(-1)?.slice(0, 10)
  return { lead: last ? before.filter(s => s.slice(0, 10) === last) : [], inMonth }
}

const iso = (t: number): string => new Date(t).toISOString().slice(0, 10)

/** The scans before ``before`` the OP window draws: from the Monday of `windowStart`'s week on, after the last day's
 *  scans before that, whole days (`window_scans`). */
export function windowScans(scans: string[], month: string, before: string): string[] {
  const inMonth = scans.filter(s => s.startsWith(`${month}-`))
  if (!inMonth.length) return []
  const mon = iso(weekOf(windowStart(monthOf(month), dayOf(inMonth.at(-1)!.slice(0, 10)), WINDOW_DAYS)))
  const older = scans.filter(s => s.slice(0, 10) < mon)
  const first = older.length ? older.at(-1)!.slice(0, 10) : mon
  return scans.filter(s => first <= s.slice(0, 10) && s < before)
}

/** A meta's primary totals and the other buckets' sizes in ``unit`` bytes, in meta order (`primary_totals`). */
export function primaryTotals(m: ScanMeta, primary: string, unit: number): { bytes: number; objs: number; extra: Record<string, number> } {
  const bk = m.buckets ?? {}
  if (primary in bk) {
    const extra: Record<string, number> = {}
    for (const [b, v] of Object.entries(bk)) if (b !== primary) extra[b] = v.total_bytes / unit
    return { bytes: Math.trunc(bk[primary].total_bytes), objs: Math.trunc(bk[primary].total_objects), extra }
  }
  return { bytes: Math.trunc(m.total_bytes), objs: Math.trunc(m.total_objects), extra: {} }
}

/** The primary's per-storage-class sizes in ``unit`` bytes, unrounded; null when the meta has none (`primary_classes`). */
export function primaryClasses(m: ScanMeta, primary: string, unit: number): Record<string, number> | null {
  const bk = m.buckets ?? {}
  const cb = primary in bk ? bk[primary].class_bytes : m.class_bytes
  if (cb == null) return null
  const out: Record<string, number> = {}
  for (const [c, b] of Object.entries(cb)) out[String(Math.trunc(Number(c)))] = b / unit
  return out
}

/** Each scan's canonical `?d=` slug among ``dated``'s scans, as of ``now`` (`scan_slugs`). */
export function scanSlugs(dated: [string, ScanMeta][], now: Date): Record<string, string> {
  const scans = dated.map(([s]) => s)
  const meta = Object.fromEntries(dated)
  const times: Record<string, string> = {}
  for (const s of timesNeeded(scans)) {
    const k = startKey(s, meta[s].started)
    if (k) times[s] = k
  }
  return Object.fromEntries(scans.map(s => [s, minSlug(s, scans, times, now)]))
}

/** ``dated`` (scan order) as the renderer's rows: sizes in ``unit`` bytes, the primary's rounded to 0.1 as the job
 *  rounds them (`rows_from_meta` → `QuotaTemplate.rows_json`). */
export function rowsFromMeta(dated: [string, ScanMeta][], primary: string, unit: number, now: Date): QuotaScan[] {
  const slugs = scanSlugs(dated, now)
  return dated.map(([scan, m]) => {
    const { bytes, objs, extra } = primaryTotals(m, primary, unit)
    return {
      scan, slug: slugs[scan], tb: pyRound(bytes / unit, 1),
      extra: Object.fromEntries(Object.entries(extra).map(([b, v]) => [b, pyRound(v, 1)])),
      classes: primaryClasses(m, primary, unit), objs,
    }
  })
}

/** A `scan_runs` row, as far as `scan_triggers` reads it. */
export interface RunTrigger { scan: string; trigger: string | null; started_ts: number | null }

/** Each scan's trigger: its first run's by start (a hand resume of a cron scan doesn't make it manual); a scan whose
 *  first run recorded none is absent (`scan_triggers`). */
export function scanTriggers(runs: Iterable<RunTrigger>): Record<string, string> {
  const sorted = [...runs].map((r, i) => [r, i] as const).sort(([a, i], [b, j]) =>
    Number(a.started_ts === null) - Number(b.started_ts === null) || (a.started_ts ?? 0) - (b.started_ts ?? 0) || i - j)
  const first = new Map<string, RunTrigger>()
  for (const [r] of sorted) if (!first.has(r.scan)) first.set(r.scan, r)
  return Object.fromEntries([...first].filter(([, r]) => r.trigger).map(([s, r]) => [s, r.trigger!]))
}

/** The ids a month's rows are read from, in order: the window's earlier scans (`pre`), the lead-in day, the month's
 *  own (`load_month`'s reads). Null: the month has no scans. */
export function monthIds(all: string[], month: string, asOf: string | null): { pre: string[]; lead: string[]; inMonth: string[]; closed: boolean } | null {
  const scans = scansAsOf(all, asOf)
  const w = windowIds(scans, month)
  if (!w) return null
  const pre = windowScans(scans, month, (w.lead.length ? w.lead : w.inMonth)[0])
  return { pre, ...w, closed: scans.at(-1)! > w.inMonth.at(-1)! }
}

/** The month's `QuotaRows` from its metas (``metas``: at least every id `monthIds` names), as of ``now`` (`load_month`
 *  → `rows_json`). */
export function monthRows(ids: { pre: string[]; lead: string[]; inMonth: string[]; closed: boolean }, metas: Readonly<Record<string, ScanMeta>>,
  primary: string, unit: number, now: Date): QuotaRows {
  const dated = [...ids.pre, ...ids.lead, ...ids.inMonth].map(s => [s, metas[s]] as [string, ScanMeta])
  const rows = rowsFromMeta(dated, primary, unit, now)
  const n = ids.pre.length, k = n + ids.lead.length
  return { template: 'quota', closed: ids.closed, pre: rows.slice(0, n), lead: rows.slice(n, k), rows: rows.slice(k) }
}
