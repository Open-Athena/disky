import type { Meta } from './types'

/** The lens's day axis: the access window (`min`, `max` epoch days), optionally with its scale (`t`). */
export interface ReadAxis { min: number, max: number, t?: (d: number) => number }

/** Whether the access logs cover a top-level cell (a source bucket: a store unioned at ingest names its
 *  buckets as its first path segment). `meta.access.buckets` lists the covered ones; a scan without it
 *  (published before the field) is covered everywhere. Any other first segment, a folded `(+N)` cell
 *  included, is unlogged when the list exists: its reads, if any, are still shown, but a missing read
 *  is "not logged", never "no observed reads". */
export function readLogged(access: Meta['access'] | undefined): (bucket: string | undefined) => boolean {
  const buckets = access?.buckets
  if (!buckets) return () => true
  const set = new Set(buckets)
  return bucket => bucket != null && set.has(bucket)
}

const DAY_MS = 86400_000
const HOUR_MS = 3600_000

/** Where a scan's read data ends (`meta.access.through`, the newest log hour it includes; spec
 *  `access-log-freshness.md` §B/§C): `day` its epoch day, `label` the hour for display, `provisional`
 *  when the scan was taken after that hour ended (reads in the gap weren't delivered yet). */
export interface ReadThrough { day: number, label: string, provisional: boolean }

/** `access.through` on the lens's terms; `scanTime` (ISO) is when the scan was taken, if known. A scan
 *  published before the field falls back to its window's last day (`to`, the newest read's day), at day
 *  precision: its logs end no earlier. Null with no access window. */
export function readThrough(access: Meta['access'] | undefined, scanTime?: string | null): ReadThrough | null {
  const through = access?.through
  const scanMs = scanTime ? Date.parse(scanTime) : NaN
  if (!through) {
    if (access?.to == null) return null
    const end = (access.to + 1) * DAY_MS
    return {
      day: access.to,
      label: new Date(access.to * DAY_MS).toISOString().slice(0, 10),
      provisional: !Number.isNaN(scanMs) && end < scanMs,
    }
  }
  const ms = Date.parse(through)
  if (Number.isNaN(ms)) throw new Error(`access.through: not a time: ${through}`)
  return {
    day: Math.floor(ms / DAY_MS),
    label: `${new Date(ms).toISOString().slice(0, 16).replace('T', ' ')}Z`,
    provisional: !Number.isNaN(scanMs) && ms + HOUR_MS < scanMs,
  }
}

/** The read window's end, as the lens says it: "last access data: <ts>", and the gap to the scan when
 *  there is one ("reads after <ts> not yet delivered"), else null. */
export const readThroughText = (t: ReadThrough): { through: string, gap: string | null } => ({
  through: `last access data: ${t.label}`,
  gap: t.provisional ? `reads after ${t.label} not yet delivered` : null,
})

/** The read lens's state of one cell (spec `access-log-freshness.md` §C, as refined by Ryan):
 *  - `read`: read since its last write: at `t` ∈ [0, 1] on the access window (old → new) by its last read.
 *  - `written`: written since logging began and not read since that write: its own look (cross-hatched),
 *    at `t` by its write day, so fresh data never reads as "never read".
 *  - `never`: logged, no read observed, and last written before logging began.
 *  - `unlogged`: no logs cover it. */
export type ReadState =
  | { kind: 'read', t: number }
  | { kind: 'written', t: number }
  | { kind: 'never' }
  | { kind: 'unlogged' }

/** Whether a write on day `d` may postdate the logs' end (`through`, an epoch day). The data's reads are
 *  all at or before `through` by construction, but a write on `through`'s own day may be after its hour
 *  (`d` is a day), so it counts as after: a sweep candidate must be provably idle. */
export const writtenAfter = (d: number | null | undefined, through: number | null | undefined): boolean =>
  d != null && through != null && d >= through

/** Whether a cell was written since logging began (`from`) and not read since that write. A read on
 *  the write's own day counts as after it (days can't order them; the read is what the logs saw). */
export const unreadSinceWrite = (a: number | null | undefined, d: number | null | undefined, from: number): boolean =>
  d != null && d >= from && (a == null || a < d)

/** A cell's last touch, `max(last read, last write)` (spec §C), as an epoch day: a write counts from
 *  logging's start (`from`; an older one says nothing about reads since). Null = nothing in the window.
 *  It is the day the lens places the cell at (its color, and its brushing value on the `a` axis). */
export function lastTouch(a: number | null | undefined, d: number | null | undefined, from: number): number | null {
  const w = d != null && d >= from ? d : null
  return a == null ? w : w == null ? a : Math.max(a, w)
}

const onAxis = (range: ReadAxis, day: number): number => {
  const t = range.max > range.min ? (range.t ? range.t(day) : (day - range.min) / (range.max - range.min)) : 1
  return Math.min(1, Math.max(0, t))
}

/** A cell's read state. A day on a one-day window (the window opens on a collector's first day) sits at
 *  the new end (`t` 1): there is no span to place it on. `d` is the cell's last write day (absent: the
 *  lens reads alone); an unlogged cell keeps its reads alone (a write says nothing about reads nobody
 *  logs). */
export function readState(
  a: number | null | undefined,
  logged: boolean,
  range: ReadAxis | null | undefined,
  d?: number | null,
): ReadState {
  if (range && logged && unreadSinceWrite(a, d, range.min)) return { kind: 'written', t: onAxis(range, d!) }
  if (a != null && range) return { kind: 'read', t: onAxis(range, a) }
  return logged ? { kind: 'never' } : { kind: 'unlogged' }
}

/** The cross-hatch the read lens draws over a `written` cell (its fill is the write's age color). */
export const WRITTEN_HATCH = 'repeating-linear-gradient(45deg, rgb(0 0 0 / 0.3) 0 1.5px, transparent 1.5px 7px), repeating-linear-gradient(135deg, rgb(0 0 0 / 0.3) 0 1.5px, transparent 1.5px 7px)'

/** The staging / deletion-review idle rule (spec §C): a path is idle only when its last read and its
 *  last write both fall before the logs' end, and "idle N days" counts from `through`, not the scan. N
 *  is a lower bound: with no read logged, the window's start stands in for the read (reads before
 *  logging began are unknown). */
export type Idleness = { idle: true, days: number } | { idle: false }

/** `a` / `d` the path's last read / write days. Null when the rule can't apply: no `through` (a scan
 *  published before the field) or no logs cover the path (`logged` false). */
export function idleness(
  a: number | null | undefined,
  d: number | null | undefined,
  access: Meta['access'] | undefined,
  logged = true,
): Idleness | null {
  const t = readThrough(access)
  if (!t || !access || !logged) return null
  if (writtenAfter(d, t.day)) return { idle: false }
  return { idle: true, days: t.day - Math.max(a ?? access.from, d ?? access.from) }
}

/** An idleness, as a table cell says it: "idle N d" (a lower bound, counted from `through`), or
 *  "written after logs" (not idle: written on or after the logs' end, reads unknown). */
export const idleLabel = (i: Idleness): string => i.idle ? `idle ${i.days}d` : 'written after logs'

/** An idleness as a sort value: idle days, a not-idle path below every idle one, and no verdict
 *  (`undefined`) last either way. */
export const idleSortKey = (i: Idleness | null): number | undefined => i == null ? undefined : i.idle ? i.days : -1

/** The bucket a `scheme://bucket/…` prefix is under (the read lens's coverage key). */
export const prefixBucket = (prefix: string): string => prefix.replace(/^[a-z0-9]+:\/\/\/?/, '').split('/')[0]

/** The view's top-level cells the logs don't cover (names, in tree order): the "not logged" legend's list. */
export function unloggedOf(names: readonly string[], logged: (bucket: string) => boolean): string[] {
  return names.filter(n => !n.startsWith('(') && !logged(n))
}
