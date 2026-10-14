import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { LINEAGES } from '../_lib/testD1'
import { A, B, C, unionDeployment, Z } from '../_lib/testUnion'
import { onRequestGet as series } from './series'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// `/api/series` on a union view (specs/federated-scans.md, "Diffs and over-time"): the members' own series as one
// step function — at each instant any member scanned, each carries its latest point; before its first, it is
// missing (the total partial), never zero. The deployment (`_lib/testUnion.ts`): the primary scans at Z, A, C;
// meta at A, B.

type Pt = { date: string; b: number; o: number; missing?: string[] }
type Body = { path: string; points: Pt[]; roots?: { path: string; points: Pt[] }[]; union?: unknown }

describe.each(LINEAGES)('D1, %s lineage', lineage => {
  let deployment: (views?: Record<string, unknown>) => Env

  beforeAll(async () => {
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
    deployment = await unionDeployment(lineage)
  })

  const get = async (qs: string, views?: Record<string, unknown>) => {
    const r = await series({ request: new Request(`http://localhost/api/series?${qs}`), env: deployment(views) })
    return { status: r.status, text: await r.text() }
  }
  const body = async (qs: string, views?: Record<string, unknown>) => {
    const r = await get(qs, views)
    expect([r.status, r.status === 200 ? '' : r.text]).toEqual([200, ''])
    return JSON.parse(r.text) as Body
  }

  it('without a main view, the root series is the primary’s alone, as before', async () => {
    expect((await get('path=', { all: { union: ['primary', 'meta'] } })).text).toBe((await get('path=', {})).text)
  })

  it('the root: one step per union event, each store’s latest point carried, a store before its first named missing', async () => {
    const [u, p, m] = await Promise.all([body('path='), body('path=&store=primary'), body('path=&store=meta')])
    // Every scan of a store reads the same fixture: one value per store.
    const [P] = p.points, [M] = m.points
    expect([p.points.map(x => x.date), m.points.map(x => x.date)]).toEqual([[Z, A, C], [A, B]])
    expect(u).toEqual({
      path: '',
      points: [
        { date: Z, b: P.b, o: P.o, missing: ['meta'] },
        { date: A, b: P.b + M.b, o: P.o + M.o },
        { date: B, b: P.b + M.b, o: P.o + M.o },
        { date: C, b: P.b + M.b, o: P.o + M.o },
      ],
      union: { view: 'all', sources: [{ store: 'primary', from: Z }, { store: 'meta', from: A }] },
    })
  })

  it('`split=roots`: every store’s roots, each at its store’s carried point, a root starting at its store’s first scan', async () => {
    const [u, p, m] = await Promise.all([body('path=&split=roots'), body('path=&split=roots&store=primary'), body('path=&split=roots&store=meta')])
    const at = (b: Body, root: string) => b.roots!.find(r => r.path === root)!.points[0]
    const [bk, zz, mb] = [at(p, 'bk'), at(p, 'zz'), at(m, 'mb')]
    const pt = (date: string, r: Pt) => ({ date, b: r.b, o: r.o })
    expect(u.roots).toEqual([
      { path: 'bk', points: [Z, A, B, C].map(d => pt(d, bk)) },
      { path: 'zz', points: [Z, A, B, C].map(d => pt(d, zz)) },
      { path: 'mb', points: [A, B, C].map(d => pt(d, mb)) },
    ])
    expect(u.points.map(x => [x.date, x.b, x.missing ?? null])).toEqual([
      [Z, bk.b + zz.b, ['meta']],
      [A, bk.b + zz.b + mb.b, null],
      [B, bk.b + zz.b + mb.b, null],
      [C, bk.b + zz.b + mb.b, null],
    ])
  })

  it('below the root: the owning store’s own series', async () => {
    for (const [path, store] of [['mb', 'meta'], ['mb/scans', 'meta'], ['bk', 'primary']] as const) {
      const [u, own] = await Promise.all([get(`path=${path}`), get(`path=${path}&store=${store}`)])
      expect([path, u.status, u.text]).toEqual([path, 200, own.text])
    }
    expect(await get('path=nope')).toEqual({ status: 404, text: '{"error":"path not found"}\n' })
  })

  it('the root refuses one store’s scope axes', async () => {
    expect(await get('path=&o=unowned&paths=bk')).toEqual({ status: 400, text: '{"error":"a union view\'s root is unscoped (o, paths: drill into one store)"}\n' })
  })
})
