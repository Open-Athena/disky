import { describe, expect, it } from 'vitest'
import { matchEntries, matchLabel, type NameMatches } from './folderSearch'

describe('matchLabel', () => {
  it('the name, then where it is', () => {
    expect(matchLabel('grug')).toBe('grug')
    expect(matchLabel('bk/users/grug')).toBe('grug — bk/users/')
  })
  it('a long parent elided from the middle, the name kept whole', () => {
    const got = matchLabel('marin-us-central2/checkpoints/experiments/scaling/run-0042/grug', 48)
    expect(got).toBe('grug — marin-us-central2/checkpoin…ing/run-0042/')
    expect(got.length).toBe(48)
  })
})

describe('matchEntries', () => {
  const size = (b: number) => `${b} B`
  const body: NameMatches = {
    date: 'D', path: '', q: 'grug', total: 3, complete: true,
    items: [
      { path: 'bk/a/grug', kind: 'dir', b: 900, o: 12 },
      { path: 'bk/b/grug.txt', kind: 'file', b: 50, o: 1 },
      { path: 'bk/c/grug', kind: null, b: 7, o: 1 },
    ],
  }
  it('one row per match: name and place, kind and size (a folder\'s objects), its path searchable', () => {
    const picked: string[] = []
    const got = matchEntries(body, { go: m => picked.push(m.path), filter: () => picked.push('filter'), size })
    expect(got.map(e => ({ id: e.id, label: e.label, description: e.description, keywords: e.keywords }))).toEqual([
      { id: 'match:bk/a/grug', label: 'grug — bk/a/', description: 'folder · 900 B · 12 objects', keywords: ['bk/a/grug'] },
      { id: 'match:bk/b/grug.txt', label: 'grug.txt — bk/b/', description: 'file · 50 B', keywords: ['bk/b/grug.txt'] },
      { id: 'match:bk/c/grug', label: 'grug — bk/c/', description: 'folder · 7 B', keywords: ['bk/c/grug'] },
    ])
    for (const e of got) e.handler!()
    expect(picked).toEqual(['bk/a/grug', 'bk/b/grug.txt', 'bk/c/grug'])
  })
  it('an incomplete answer ends with its reason, which filters the map', () => {
    const picked: string[] = []
    const got = matchEntries({ ...body, items: [], total: 90_000, complete: false, reason: 'Too many matches to list.' },
      { go: () => picked.push('go'), filter: () => picked.push('filter'), size })
    expect(got.map(e => [e.id, e.label, e.description])).toEqual([['match:incomplete', 'Too many matches to list.', 'Pick to filter the map instead']])
    got[0].handler!()
    expect(picked).toEqual(['filter'])
  })
})
