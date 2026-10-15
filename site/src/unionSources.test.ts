import { describe, expect, it } from 'vitest'
import { cutoffDays } from './federatedPickerModel'
import { cutoffOfScan, marksOf, parseUnionScans, refusedSources, rootSource, scanAtCutoff, seriesMissing, sourcesBefore } from './unionSources'

// r2's shape (specs/federated-scans.md, "First live cases" 1): the primary and `/meta`, two stores of one
// deployment that share scan ids (date-only, at their midnights); meta has an icon, the primary none.
const body = { union: { view: 'all', sources: [
  { store: 'primary', label: 'Public R2 buckets', scans: [
    { id: '2026-10-14', at: '2026-10-14T00:00:00.000Z', day: true as const },
    { id: '2026-10-13', at: '2026-10-13T00:00:00.000Z', day: true as const },
  ] },
  { store: 'meta', label: 'Index', icon: '/icons/meta.svg', scans: [
    { id: '2026-10-14', at: '2026-10-14T00:00:00.000Z', day: true as const },
  ] },
] } }
const u = parseUnionScans(body)!
const marks = marksOf(u)

describe('union picker sources', () => {
  it('`/api/union-scans` → picker sources; `{union: null}` → none (the single-store picker)', () => {
    expect(u).toEqual({ view: 'all', sources: [
      { source: 'primary', label: 'Public R2 buckets', scans: [
        { id: '2026-10-14', revision: '', observed_through: '2026-10-14T00:00:00.000Z', published_at: '2026-10-14T00:00:00.000Z', day: true },
        { id: '2026-10-13', revision: '', observed_through: '2026-10-13T00:00:00.000Z', published_at: '2026-10-13T00:00:00.000Z', day: true },
      ] },
      { source: 'meta', label: 'Index', icon: '/icons/meta.svg', scans: [
        { id: '2026-10-14', revision: '', observed_through: '2026-10-14T00:00:00.000Z', published_at: '2026-10-14T00:00:00.000Z', day: true },
      ] },
    ] })
    expect(parseUnionScans({ union: null })).toBe(null)
  })
  it('shared ids are one chip per instant carrying both marks (meta’s icon, the primary’s label); date-only ids read as their dates in any zone', () => {
    const want = [
      { day: '2026-10-14', events: [{ at: '2026-10-14T00:00:00.000Z', time: 'Oct 14', day: true, sources: [
        { source: 'meta', label: 'Index', icon: '/icons/meta.svg' }, { source: 'primary', label: 'Public R2 buckets' },
      ] }] },
      { day: '2026-10-13', events: [{ at: '2026-10-13T00:00:00.000Z', time: 'Oct 13', day: true, sources: [{ source: 'primary', label: 'Public R2 buckets' }] }] },
    ]
    expect([cutoffDays(u.sources, 'UTC'), cutoffDays(u.sources, 'America/New_York')]).toEqual([want, want])
  })
  it('scan id ↔ cutoff: a picked instant reads as the newest observation through it, among the page’s scans', () => {
    expect([
      cutoffOfScan(u.sources, '2026-10-13'),
      cutoffOfScan(u.sources, '2026-10-01'),
      scanAtCutoff(u.sources, '2026-10-14T00:00:00.000Z'),
      scanAtCutoff(u.sources, '2026-10-13T18:30:00.000Z'),
      scanAtCutoff(u.sources, '2026-10-12T00:00:00.000Z'),
      scanAtCutoff(u.sources, '2026-10-14T09:00:00.000Z', new Set(['2026-10-13'])),
    ]).toEqual(['2026-10-13T00:00:00.000Z', null, '2026-10-14', '2026-10-13', null, '2026-10-13'])
    expect(sourcesBefore(u.sources, '2026-10-14T00:00:00.000Z').map(s => [s.source, s.scans.map(o => o.id)])).toEqual([['primary', ['2026-10-13']], ['meta', []]])
  })
})

describe('union answers on the page', () => {
  it('a root’s store and scan (`src`/`at`); a node without them has none; an unlisted store is its key', () => {
    expect([
      rootSource({ src: 'meta', at: '2026-10-14' }, marks),
      rootSource({ src: 'primary', at: '2026-10-14' }, marks),
      rootSource({}, marks),
      rootSource({ src: 'other', at: '2026-10-14' }, marks),
    ]).toEqual([
      { mark: { source: 'meta', label: 'Index', icon: '/icons/meta.svg' }, at: '2026-10-14' },
      { mark: { source: 'primary', label: 'Public R2 buckets' }, at: '2026-10-14' },
      null,
      { mark: { source: 'other', label: 'other' }, at: '2026-10-14' },
    ])
  })
  it('a member refusing the filter is named with its reason; members answering are not', () => {
    const vector = { view: 'all', at: '2026-10-14T00:00:00.000Z', sources: [
      { store: 'primary', date: '2026-10-14' },
      { store: 'meta', date: '2026-10-14', refused: { code: 'scan-not-indexed', error: 'scan 2026-10-14 is not in the name index' } },
    ] }
    expect([refusedSources(vector, marks), refusedSources({ view: 'all', sources: [{ store: 'primary', date: '2026-10-14' }] }, marks), refusedSources(undefined, marks)]).toEqual([
      [{ mark: { source: 'meta', label: 'Index', icon: '/icons/meta.svg' }, code: 'scan-not-indexed', error: 'scan 2026-10-14 is not in the name index' }],
      [],
      [],
    ])
  })
  it('a series’ points missing a member: that member, through its last missing point', () => {
    expect(seriesMissing([
      { date: '2026-10-12', missing: ['meta'] },
      { date: '2026-10-13', missing: ['meta'] },
      { date: '2026-10-14' },
    ], marks)).toEqual([{ mark: { source: 'meta', label: 'Index', icon: '/icons/meta.svg' }, through: '2026-10-13' }])
    expect(seriesMissing([{ date: '2026-10-14' }], marks)).toEqual([])
  })
})
