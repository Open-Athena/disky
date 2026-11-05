import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { sqliteD1 } from './testD1'
import { fixtureR2 as r2 } from './testR2'
import { readJson } from './testStore'
import { buildDiff, type Diff, type DiffRow } from './view'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// The as-of walk with range churn on its rows (`buildDiff` `churn`, specs/diff-progressive.md §3c), over
// `fixtures/iv-churn/`: a base over four scans and three appended runs, 4 rows per group. `expected.json` is the churn
// oracle: per `(from, to, path)` from the churn base on, each changed child's values and range churn, computed from the
// scans' files alone. Files drop out of scans and come back (`_files`), and two trade sizes (a same-size swap).

type WantRow = { p: string; k: 'file' | 'dir'; a: number; b: number; oa: number; ob: number; churn: number }
type Want = { from: string; to: string; path: string; a: number; b: number; oa: number; ob: number; churn: number; rows: WantRow[] }

let env: Env
let cases: Want[]
let oracle: Map<string, { churn: number; a: number; b: number }>

const key = (from: string, to: string, p: string) => `${from}|${to}|${p}`
const walk = (c: { from: string; to: string; path: string }, threshold: number, churn = true, trace?: (name: string, v: number) => void): Promise<Diff> =>
  buildDiff(env, { from: c.from, to: c.to, path: c.path, w: 1024, h: 1024, minArea: 12, atten: 1, top: 5000, threshold, ...(churn ? { churn } : {}), ...(trace ? { trace } : {}) })
const isOther = (r: DiffRow) => r.p === '(other)' || r.p.endsWith('/(other)')
const parentOf = (p: string) => (p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '')
const abs = (path: string, p: string) => (path ? `${path}/${p}` : p)
const byP = <T extends { p: string }>(rows: T[]): T[] => [...rows].sort((x, y) => (x.p < y.p ? -1 : 1))

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const { db } = await sqliteD1()
  env = { DB: db, ROOT_LABEL: 'all buckets', PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g', INDEX_R2: r2('iv-churn', []) } as Env
  cases = await readJson<Want[]>('iv-churn/expected.json')
  oracle = new Map()
  for (const c of cases) {
    oracle.set(key(c.from, c.to, c.path), { churn: c.churn, a: c.a, b: c.b })
    for (const r of c.rows) oracle.set(key(c.from, c.to, r.p), { churn: r.churn, a: r.a, b: r.b })
  }
})

describe('the churn walk', () => {
  it('with no floor, draws every changed child with the oracle\'s exact churn, renames and one-sided paths among them', async () => {
    expect(cases.length).toBe(60)
    const got = []
    let oneSided = 0, reappeared = 0
    for (const c of cases.filter(x => x.a > 0 && x.b > 0)) {
      const d = await walk(c, 0)
      expect(d.churn_off).toBeUndefined()
      expect(d.churn).toBe(c.churn)
      const kids = d.rows.filter(r => r.d === 1 && !isOther(r) && (r.c || r.s !== 'unchanged'))
      got.push({ from: c.from, to: c.to, path: c.path, rows: byP(kids.map(r => ({ p: abs(c.path, r.p), k: r.k, a: r.a, b: r.b, oa: r.oa, ob: r.ob, churn: r.c! }))) })
      oneSided += c.rows.filter(r => !r.a || !r.b).length
      // Gone at one end, with churn beyond what its bytes at the other end account for: it had a history before.
      reappeared += c.rows.filter(r => (!r.a || !r.b) && r.churn > r.a + r.b).length
    }
    expect(got).toEqual(cases.filter(x => x.a > 0 && x.b > 0).map(c => ({ from: c.from, to: c.to, path: c.path, rows: byP(c.rows) })))
    expect([oneSided > 0, reappeared > 0]).toEqual([true, true])
  })

  it('at any floor, a drawn row\'s churn is the oracle\'s, and each `(other)` is its parent\'s less the drawn children\'s, exactly', async () => {
    const seen = { other: 0, drawn: 0 }
    for (const c of cases.filter(x => x.a > 0 && x.b > 0)) {
      for (const threshold of [0, 11, 41, 101]) {
        const d = await walk(c, threshold)
        expect(d.churn).toBe(c.churn)
        const sums = new Map<string, number>()
        for (const r of d.rows) {
          const q = parentOf(r.p)
          sums.set(q, (sums.get(q) ?? 0) + r.c!)
          if (isOther(r)) { seen.other += r.c ? 1 : 0; continue }
          seen.drawn++
          expect(r.c).toBe(oracle.get(key(c.from, c.to, abs(c.path, r.p)))?.churn ?? 0)
        }
        // Children (with `(other)`) sum to the parent, on the root and every expanded row: truthful, and never clamped.
        expect(sums.get('') ?? c.churn).toBe(c.churn)
        for (const r of d.rows.filter(x => x.x)) expect(sums.get(r.p)).toBe(r.c)
      }
    }
    expect([seen.other > 0, seen.drawn > 0]).toEqual([true, true])
  })

  it('expands a directory whose bytes held but whose churn didn\'t: the same-size swap under `b2/z`', async () => {
    const c = cases.find(x => x.path === 'b2/z' && x.a === x.b && x.oa === x.ob && x.churn > 0)!
    const parent = { from: c.from, to: c.to, path: 'b2' }
    const d = await walk(parent, 0)
    const z = d.rows.find(r => r.p === 'z')!
    expect({ s: z.s, a: z.a, b: z.b, c: z.c, x: z.x }).toEqual({ s: 'unchanged', a: c.a, b: c.b, c: c.churn, x: true })
    expect(byP(d.rows.filter(r => r.p.startsWith('z/')).map(r => ({ p: r.p, c: r.c })))).toEqual(byP(c.rows.map(r => ({ p: r.p.slice('b2/'.length), c: r.churn }))))
    // A plain walk stops at the identical bytes.
    expect((await walk(parent, 0, false)).rows.filter(r => r.p.startsWith('z/'))).toEqual([])
  })

  it('ranks leaves by churn at `top`: what\'s cut folds into `(other)`, which still sums', async () => {
    const c = cases.find(x => x.path === '' && x.rows.length >= 2)!
    const d = await buildDiff(env, { from: c.from, to: c.to, path: 'b1/x', w: 1024, h: 1024, minArea: 12, atten: 1, top: 1, threshold: 0, churn: true })
    const leaves = d.rows.filter(r => !r.x && !isOther(r))
    expect(leaves.length).toBe(1)
    const all = await walk({ from: c.from, to: c.to, path: 'b1/x' }, 0)
    expect(leaves[0].c).toBe(Math.max(...all.rows.filter(r => !r.x && !isOther(r)).map(r => r.c!)))
    expect(d.rows.filter(r => parentOf(r.p) === '').reduce((n, r) => n + r.c!, 0)).toBe(d.churn)
  })

  it('falls back to a plain walk, saying why: a range from before the churn base, a path on one scan', async () => {
    const before = { from: '2026-07-31', to: '2026-08-03', path: '' }
    const off = await walk(before, 0)
    expect(off.churn_off).toMatch(/^churn is counted from scan stamp \d+; 2026-07-31 is before it$/)
    const { churn_off: _, ...plain } = off
    expect(plain).toEqual(await walk(before, 0, false))
    expect(off.rows.filter(r => r.c != null)).toEqual([])
    const one = cases.find(x => x.path && (!x.a || !x.b))
    if (one) {
      const d = await walk(one, 0)
      expect(d.churn_off).toBe('the path is on one scan only')
      expect(d.rows.filter(r => r.c != null)).toEqual([])
    }
  })

  it('stays on the walk\'s readers: no page-index read, no `readChanged`, and no more lookups than a plain walk', async () => {
    for (const c of cases.filter(x => x.a > 0 && x.b > 0)) {
      for (const threshold of [11, 41]) {
        const names = new Set<string>()
        const d = await walk(c, threshold, true, n => names.add(n))
        expect([...names].filter(n => /^(p|lv-|level)/.test(n))).toEqual([])
        const plain = await walk(c, threshold, false)
        // Lookups beyond the plain walk's are only under a directory it wouldn't expand (identical bytes, churn moved).
        if (d.expansions === plain.expansions) expect(d.lookups).toBe(plain.lookups)
      }
    }
  })

  it('leaves a plain walk\'s rows as they are: no churn columns, no `c`', async () => {
    const c = cases.find(x => x.path === '')!
    const names: string[] = []
    const d = await walk(c, 11, false, n => names.push(n))
    expect(d.rows.filter(r => r.c != null)).toEqual([])
    expect('churn' in d).toBe(false)
  })
})
