/** A union view on the page (specs/federated-scans.md, "First live cases" 1): its members as the scan picker's
 *  sources (`/api/union-scans`), the scan id ↔ cutoff mapping the picker needs, and the union fields the API
 *  tags its answers with — a root's store and scan (`src`/`at`), a series point's uncovered members
 *  (`missing`), a member's refused filter (`union.sources[].refused`) — as what the page shows. */
import { createContext, useContext } from 'react'
import { useQuery } from '@tanstack/react-query'
import { compareScans, type Observation } from './federatedScans'
import { markOf, type PickerSource, type SourceMarkInfo } from './federatedPickerModel'
import { storeUrl, type Store } from './stores'

/** A union view's members as picker sources (`source` = the member's store key). */
export interface UnionSources { view: string; sources: PickerSource[] }

interface Wire { union: null | { view: string; sources: { store: string; label: string; icon?: string; scans: { id: string; at: string; day?: true }[] }[] } }

/** `/api/union-scans`'s body as picker sources; null = no union view (the single-store picker). */
export function parseUnionScans(body: Wire): UnionSources | null {
  if (!body.union) return null
  return {
    view: body.union.view,
    sources: body.union.sources.map(s => ({
      source: s.store, label: s.label, ...(s.icon ? { icon: s.icon } : {}),
      scans: s.scans.map(o => ({ id: o.id, revision: '', observed_through: o.at, published_at: o.at, ...(o.day ? { day: true as const } : {}) })),
    })),
  }
}

/** The union view `store`'s requests read, if any (null while loading, on a failure, or with none: the
 *  single-store picker stays, so a deployment without a view is unchanged). */
export function useUnionSources(store: Store): UnionSources | null {
  const q = useQuery({
    queryKey: ['union-scans', store.key],
    queryFn: async () => {
      const r = await fetch(storeUrl('/api/union-scans', store), { credentials: 'include' })
      if (!r.ok) throw new Error(`union-scans: ${r.status}`)
      return parseUnionScans(await r.json() as Wire)
    },
    staleTime: 5 * 60_000,
    refetchInterval: 5 * 60_000,
    retry: false,
  })
  return q.data ?? null
}

/** The page's union members' marks, by store (null: no union view). */
export const UnionMarks = createContext<ReadonlyMap<string, SourceMarkInfo> | null>(null)
export const marksOf = (u: UnionSources | null): ReadonlyMap<string, SourceMarkInfo> | null =>
  u ? new Map(u.sources.map(s => [s.source, markOf(s)])) : null
export const useUnionMarks = (): ReadonlyMap<string, SourceMarkInfo> | null => useContext(UnionMarks)
/** A store's mark; one the view doesn't list (an older response) is its key. */
export const markFor = (marks: ReadonlyMap<string, SourceMarkInfo> | null, store: string): SourceMarkInfo =>
  marks?.get(store) ?? { source: store, label: store }

/** The cutoff (UTC instant) a scan id names: the first member observing it. Null = no member has it. */
export function cutoffOfScan(sources: readonly PickerSource[], id: string): string | null {
  for (const s of sources) for (const o of s.scans) if (o.id === id) return o.observed_through
  return null
}

/** The scan id a picked cutoff reads: the newest observation through it, among `pickable` ids when given (the
 *  page's own scan list, which a `?d=` must name). The union's state only changes at observations, so any
 *  instant reads as the newest one at or before it. Null = nothing observed by then. */
export function scanAtCutoff(sources: readonly PickerSource[], at: string, pickable?: ReadonlySet<string>): string | null {
  let best: Observation | null = null
  for (const s of sources) for (const o of s.scans) {
    if (o.observed_through > at || (pickable && !pickable.has(o.id))) continue
    if (!best || compareScans(o, best) > 0) best = o
  }
  return best?.id ?? null
}

/** Sources trimmed to observations strictly before `before` (a diff's start must precede its end). */
export const sourcesBefore = (sources: readonly PickerSource[], before: string): PickerSource[] =>
  sources.map(s => ({ ...s, scans: s.scans.filter(o => o.observed_through < before) }))

/** A union root child's provenance: its store's mark and that store's scan id (null: not a union root's). */
export function rootSource(node: { src?: string; at?: string }, marks: ReadonlyMap<string, SourceMarkInfo> | null): { mark: SourceMarkInfo; at: string } | null {
  return node.src && node.at ? { mark: markFor(marks, node.src), at: node.at } : null
}

/** The `union` block a union answer carries (`_lib/union.ts` `vectorOf` / `filteredVector`): each member's
 *  scan at the cutoff, and a refusing member's refusal of the filter. */
export interface UnionVector {
  view: string
  at?: string
  sources: { store: string; date?: string | null; refused?: { code: string; error: string } }[]
}

/** A filtered union root's refusing members (`union.sources[].refused`): which store, and why. */
export interface Refused { mark: SourceMarkInfo; code: string; error: string }
export function refusedSources(union: UnionVector | undefined, marks: ReadonlyMap<string, SourceMarkInfo> | null): Refused[] {
  return (union?.sources ?? []).flatMap(s => (s.refused ? [{ mark: markFor(marks, s.store), code: s.refused.code, error: s.refused.error }] : []))
}

/** A union series' partial stretch (points naming `missing` members): each member not yet scanned, and the
 *  last scan id it was missing at (the total is partial through there). Members in first-missing order. */
export function seriesMissing(points: readonly { date: string; missing?: string[] }[], marks: ReadonlyMap<string, SourceMarkInfo> | null): { mark: SourceMarkInfo; through: string }[] {
  const through = new Map<string, string>()
  for (const p of points) for (const m of p.missing ?? []) through.set(m, p.date)
  return [...through].map(([store, date]) => ({ mark: markFor(marks, store), through: date }))
}
