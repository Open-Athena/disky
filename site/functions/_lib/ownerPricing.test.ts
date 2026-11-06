// Owner pricing off the read path (specs/owner-pricing-off-the-read-path.md): the pipeline fills
// `owner_prefix_hist` (`POST /api/owner-prices`: after each scan, at a ledger write, and carried to a compaction's
// new generation), and no read prices: a fold answers from one bounded D1 read, or says what isn't priced.
//
// Over `fixtures/iv/interval-store`: `g2` (one generation of three scans) and `g5` (a base of two and a run of the
// third) hold the same scans as the per-scan fixtures `v2-slices`, `v2-lens`, `v2-slices`, whose per-scan pricing
// (`testOwnerPerScan.ts`: what the read path did before) is the oracle.
import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { gateFor } from './auth'
import { sqliteD1, withLedger, withoutLedger } from './testD1'
import { type D1Variant, fixture, readJson, seedGeneration } from './testStore'
import { perScanTotals } from './testOwnerPerScan'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

const [S1, S2, S3] = ['2026-09-30T0007', '2026-09-30T0008', '2026-09-30T0009']
/** The scans' ordinals (`vf`/`vt` units: their stamps). */
const [T1, T2, T3] = [1790726820, 1790726880, 1790726940]
const LEDGER = `
  INSERT INTO actions (id, actor, ts, scan, pattern, set_owner, owner) VALUES
    (1, 'ops', 1, '${S2}', 'gs://bk/m/', 1, 'alice'), (2, 'ops', 2, '${S2}', 'gs://bk/', 1, 'bob'), (3, 'ops', 3, '${S2}', 'gs://bk/nest/', 1, 'carol');
  INSERT INTO owner_prefixes (action_id, prefix, owner, ts) VALUES (1, 'gs://bk/m/', 'alice', 1), (2, 'gs://bk/', 'bob', 2), (3, 'gs://bk/nest/', 'carol', 3);
`
const assign = (id: number, prefix: string, owner: string) => `
  INSERT INTO actions (id, actor, ts, scan, pattern, set_owner, owner) VALUES (${id}, 'ops', ${id}, '${S2}', '${prefix}', 1, '${owner}');
  INSERT INTO owner_prefixes (action_id, prefix, owner, ts) VALUES (${id}, '${prefix}', '${owner}', ${id});
`

type Fs = { existsSync(p: string): boolean; readFileSync(p: string): Uint8Array; readdirSync(p: string, o?: { recursive: true }): string[]; statSync(p: string): { isFile(): boolean; isDirectory(): boolean } }
const fs = async () => (await import(/* @vite-ignore */ 'node:fs' as string)) as Fs
/** Every key `INDEX_R2` served, in order. */
const r2Reads: string[] = []
/** The store's row data (a served sort's parquet, not its footer file): what a pricing read decodes. */
const dataReads = (from: number) => r2Reads.slice(from).filter(k => /\/served\/[^/]+\.parquet$/.test(k) && !k.endsWith('.groups.parquet'))

/** A store whose runs aren't published yet: its manifests don't list (a generation then holds its base's scans alone). */
let unpublished = false

/** `INDEX_R2` over `fixtures/iv/` (`get`, `head`, and `list` with a delimiter: the store's generations). */
function r2(): R2Bucket {
  const get = async (key: string, o?: { range?: { offset: number; length: number } | { suffix: number } }) => {
    const f = await fs()
    const file = fixture(`iv/${key}`)
    if (!f.existsSync(file)) return null
    r2Reads.push(key)
    const buf = f.readFileSync(file)
    const r = o?.range
    const { offset = 0, length = buf.byteLength - offset } = r && 'suffix' in r ? { offset: Math.max(0, buf.byteLength - r.suffix) } : r ?? {}
    const part = buf.slice(offset, offset + length)
    return { size: buf.byteLength, arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength), json: async () => JSON.parse(new TextDecoder().decode(part)) }
  }
  const list = async ({ prefix, delimiter, cursor }: { prefix: string; delimiter?: string; cursor?: string }) => {
    const f = await fs()
    const dir = prefix.slice(0, prefix.lastIndexOf('/') + 1)
    const root = fixture(`iv/${dir}`)
    if (unpublished && prefix.endsWith('/manifests/')) return { objects: [], delimitedPrefixes: [], truncated: false, cursor }
    if (delimiter) {
      const dirs = f.existsSync(root) ? f.readdirSync(root).filter(x => f.statSync(`${root}${x}`).isDirectory()).sort() : []
      return { objects: [], delimitedPrefixes: dirs.map(d => `${dir}${d}/`), truncated: false, cursor }
    }
    const keys = f.existsSync(root) ? f.readdirSync(root, { recursive: true }).map(x => `${dir}${x}`).filter(k => k.startsWith(prefix) && f.statSync(fixture(`iv/${k}`)).isFile()).sort() : []
    return { objects: keys.map(key => ({ key })), delimitedPrefixes: [], truncated: false, cursor }
  }
  const head = async (key: string) => ((await fs()).existsSync(fixture(`iv/${key}`)) ? { key } : null)
  return { get, list, head } as unknown as R2Bucket
}

const files = (dir: string, sorts: Record<string, string>) =>
  Object.fromEntries(Object.entries(sorts).map(([v, stem]) => [v, { parquet: `${dir}/${stem}.parquet`, groups: `${dir}/${stem}.groups.json` }]))

/** D1's statements, classified: what a read costs and whether anything wrote. */
const sqlLog: string[] = []
const kindOf = (sql: string): string =>
  /^\s*(INSERT|DELETE|UPDATE)\b/i.test(sql) ? `write ${/(?:INTO|FROM|UPDATE)\s+(\w+)/i.exec(sql)![1]}`
    : /owner_prefix_hist/.test(sql) ? 'read owner_prefix_hist'
      : /owner_prefix_aggs/.test(sql) ? 'read owner_prefix_aggs'
        : /MAX\(id\)/.test(sql) ? 'head'
          : /FROM owner_prefixes o JOIN actions/.test(sql) ? 'ledger'
            : 'other'
const logged = <T extends { prepare(sql: string): unknown }>(db: T): T => new Proxy(db, { get(t, k) {
  const v = Reflect.get(t, k)
  if (k !== 'prepare') return typeof v === 'function' ? v.bind(t) : v
  return (sql: string) => { sqlLog.push(kindOf(sql)); return (v as (s: string) => unknown).call(t, sql) }
} })

/** The per-scan stores and the interval store (generation `gen`) over the same scans, on one D1 holding the gcs
 *  ledger's tables with `ledger` (SQL) in them. */
async function seed(ledger = LEDGER, gen = 'g2'): Promise<{ perScan: Env; iv: Env; raw: Awaited<ReturnType<typeof sqliteD1>>['raw'] }> {
  const { db, raw } = await sqliteD1()
  const slicesD1 = await readJson<Record<string, D1Variant>>('v2-slices/d1.json')
  const lensD1 = await readJson<Record<string, D1Variant>>('v2-lens/d1.json')
  for (const date of [S1, S3]) seedGeneration(raw, { date, gen: 'g', dir: `listing/${date}/index/g`, variants: slicesD1, files: files('v2-slices', { path: 'path-index', bysize: 'path-index-bysize' }) })
  withLedger(raw)
  if (ledger) raw.exec(ledger)
  seedGeneration(raw, { date: S2, gen: 'g', dir: `listing/${S2}/index/g`, variants: lensD1, files: files('v2-lens', { path: 'path-index', bysize: 'path-index-bysize', 'bysize-user': 'path-index-bysize-by-user' }) })
  const perScan = { DB: logged(db), ROOT_LABEL: 'root', BASE_SCOPE: 'gcs', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', SESSION_SECRET: 's'.repeat(32) } as Env
  return { perScan, iv: { ...perScan, PATH_STORE: 'intervals', INTERVAL_STORE_GEN: gen, INDEX_R2: r2() } as Env, raw }
}

/** A fresh `ownerTotals.ts` (and everything it holds per isolate): as a cold isolate. */
const cold = async () => { vi.resetModules(); return import('./ownerTotals') }
type Totals = Awaited<ReturnType<typeof import('./ownerTotals').ownerTotals>>
const fold = ({ v: _v, computed: _c, ...body }: Totals) => body
const hist = (env: Env) => env.DB!.prepare('SELECT idx, depth, path, hz, versions FROM owner_prefix_hist ORDER BY idx, depth, path').all<{ idx: string; depth: number; path: string; hz: number; versions: string }>().then(r => r.results)
const count = async (env: Env, table: string) => (await env.DB!.prepare(`SELECT COUNT(*) AS n FROM ${table}`).first<{ n: number }>())!.n
/** The fill, run to its end `n` at a time: each call's `[priced, remaining]`. */
async function fillAll(m: Awaited<ReturnType<typeof cold>>, env: Env, n: number, o: { gen?: string } = {}): Promise<[number, number][]> {
  const calls: [number, number][] = []
  for (;;) {
    const got = await m.fillOwnerHist(env, n, o)
    if (!got || 'behind' in got) throw new Error(`fill: ${JSON.stringify(got)}`)
    calls.push([got.priced, got.remaining])
    if (!got.remaining) return calls
  }
}
const pending = (p: Promise<unknown>) => p.then(() => 'answered', (e: { name: string; pending?: unknown }) => (e.name === 'OwnerPricingPending' ? e.pending : `${e.name}: ${String(e)}`))

beforeAll(() => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
})

describe('the fill', () => {
  it('prices what the read path priced per scan: every scan folds as the per-scan pricing did (runs and close records too)', async () => {
    for (const gen of ['g2', 'g5']) {
      const m = await cold()
      const { perScan, iv } = await seed(LEDGER, gen)
      // Three prefixes, two a call: resumable (the second call prices the rest), idempotent (a third prices nothing).
      expect([gen, await fillAll(m, iv, 2), await m.fillOwnerHist(iv, 2)]).toEqual([gen, [[2, 1], [1, 0]], { index: `pq:iv:${gen}:s`, hz: T3, prefixes: 3, priced: 0, remaining: 0, groups: 0 }])
      for (const date of [S1, S2, S3]) {
        const got = await m.ownerTotals(iv, date)
        expect({ gen, date, index: got.computed.index, fold: fold(got) }).toEqual({ gen, date, index: `pq:iv:${gen}:s`, fold: await perScanTotals(perScan, date) })
      }
    }
  })

  it('writes one row per prefix, per root and the roots marker, each priced through the newest scan', async () => {
    const m = await cold()
    const { iv } = await seed()
    await fillAll(m, iv, 64)
    expect((await hist(iv)).map(r => [r.idx, r.depth, r.path, r.hz])).toEqual([
      ['pq:iv:g2:s', 0, '', T3], ['pq:iv:g2:s', 1, 'bk', T3], ['pq:iv:g2:s', 2, 'bk/m', T3], ['pq:iv:g2:s', 2, 'bk/nest', T3],
    ])
    expect(await count(iv, 'owner_prefix_aggs')).toBe(0)
  })

  it('waits for the scan it was asked to price: `behind` until the store holds it', async () => {
    const m = await cold()
    const { iv } = await seed()
    expect([await m.fillOwnerHist(iv, 64, { scan: '2026-10-01T0000' }), await hist(iv)]).toEqual([{ behind: '2026-10-01T0000' }, []])
    expect(await m.fillOwnerHist(iv, 64, { scan: S3 })).toEqual({ index: 'pq:iv:g2:s', hz: T3, prefixes: 3, priced: 3, remaining: 0, groups: expect.any(Number) })
  })

  it('prices a generation that is not the current one (`gen`), under its own tag', async () => {
    const m = await cold()
    const { iv } = await seed()
    expect(await fillAll(m, iv, 64, { gen: 'g5' })).toEqual([[3, 0]])
    expect([...new Set((await hist(iv)).map(r => r.idx))]).toEqual(['pq:iv:g5:s'])
  })

  it('answers null where no interval store serves the deployment', async () => {
    const m = await cold()
    const { perScan } = await seed()
    expect([await m.fillOwnerHist(perScan, 64), await m.priceAssigned(perScan, ['gs://bk/m/']), await hist(perScan)]).toEqual([null, null, []])
  })
})

describe('a read', () => {
  it('with pricing does one bounded read: one query for the generation, held for every scan, and no row data', async () => {
    const filled = await cold()
    const { iv } = await seed()
    await fillAll(filled, iv, 64)
    const m = await cold()
    const [sql0, r20] = [sqlLog.length, r2Reads.length]
    const got = [await m.ownerTotals(iv, S1), await m.ownerTotals(iv, S2), await m.ownerTotals(iv, S3)]
    expect(got.map(t => [t.scan, t.head, t.assignments.length])).toEqual([[S1, 3, 3], [S2, 3, 3], [S3, 3, 3]])
    // Each fold checks the ledger's head; the first reads the ledger and the generation's pricing, once.
    expect(sqlLog.slice(sql0)).toEqual(['head', 'ledger', 'read owner_prefix_hist', 'head', 'head'])
    expect(dataReads(r20)).toEqual([])
  })

  it('without pricing answers not-ready (which prefixes, and the roots) and prices nothing', async () => {
    const m = await cold()
    const { iv } = await seed()
    const [sql0, r20] = [sqlLog.length, r2Reads.length]
    expect(await pending(m.ownerTotals(iv, S2))).toEqual({ scan: S2, index: 'pq:iv:g2:s', why: 'pending', count: 3, prefixes: ['bk', 'bk/m', 'bk/nest'], roots: true })
    expect(await pending(m.ownerAssignments(iv, S1))).toEqual({ scan: S1, index: 'pq:iv:g2:s', why: 'pending', count: 3, prefixes: ['bk', 'bk/m', 'bk/nest'], roots: true })
    // One read of the (empty) pricing for both folds, no write, no row data read.
    expect(sqlLog.slice(sql0)).toEqual(['head', 'ledger', 'read owner_prefix_hist', 'head'])
    expect([await hist(iv), await count(iv, 'owner_prefix_aggs'), dataReads(r20)]).toEqual([[], 0, []])
  })

  it('names exactly what a partial fill left, and is ready once the fill ends', async () => {
    const m = await cold()
    const { iv } = await seed()
    expect(await m.fillOwnerHist(iv, 2)).toEqual({ index: 'pq:iv:g2:s', hz: T3, prefixes: 3, priced: 2, remaining: 1, groups: expect.any(Number) })
    expect(await pending(m.ownerTotals(iv, S2))).toEqual({ scan: S2, index: 'pq:iv:g2:s', why: 'pending', count: 1, prefixes: ['bk/nest'], roots: false })
    await fillAll(m, iv, 2)
    expect((await m.ownerTotals(iv, S2)).assignments.map(a => a.prefix).sort()).toEqual(['gs://bk/', 'gs://bk/m/', 'gs://bk/nest/'])
  })

  it('of a scan appended since the fill is not ready until the job fills again, in one isolate; the scans it priced still answer', async () => {
    // `g5`: a base of S1 and S2, and S3 as a run its manifest lists. Before the run is published the store holds two scans.
    const m = await cold()
    const { perScan, iv } = await seed(LEDGER, 'g5')
    unpublished = true
    try {
      expect(await m.fillOwnerHist(iv, 64)).toEqual({ index: 'pq:iv:g5:s', hz: T2, prefixes: 3, priced: 3, remaining: 0, groups: expect.any(Number) })
      expect(fold(await m.ownerTotals(iv, S2))).toEqual(await perScanTotals(perScan, S2))
    } finally {
      unpublished = false
    }
    // The append lands, and the isolate's view of the store has aged past its TTL.
    const now = Date.now
    const clock = vi.spyOn(Date, 'now').mockImplementation(() => now() + 61_000)
    try {
      expect(await pending(m.ownerTotals(iv, S3))).toEqual({ scan: S3, index: 'pq:iv:g5:s', why: 'pending', count: 3, prefixes: ['bk', 'bk/m', 'bk/nest'], roots: true })
      expect(fold(await m.ownerTotals(iv, S2))).toEqual(await perScanTotals(perScan, S2))
      // The scan job's fill, in the isolate that filled the scan before: every prefix is stale again (its `hz`
      // advances) and is read again, not answered from the earlier fill.
      expect(await m.fillOwnerHist(iv, 64, { scan: S3 })).toEqual({ index: 'pq:iv:g5:s', hz: T3, prefixes: 3, priced: 3, remaining: 0, groups: expect.any(Number) })
      expect((await hist(iv)).map(r => [r.path, r.hz])).toEqual([['', T3], ['bk', T3], ['bk/m', T3], ['bk/nest', T3]])
      for (const date of [S1, S2, S3]) expect(fold(await m.ownerTotals(iv, date))).toEqual(await perScanTotals(perScan, date))
    } finally {
      clock.mockRestore()
    }
  })

  it('of a scan no interval store holds is refused for good (`unheld`): pricing is per store generation', async () => {
    const m = await cold()
    const { perScan } = await seed()
    expect(await pending(m.ownerTotals(perScan, S2))).toEqual({ scan: S2, index: 'd1:g', why: 'unheld', count: 3, prefixes: ['bk', 'bk/m', 'bk/nest'], roots: true })
    expect([await hist(perScan), await count(perScan, 'owner_prefix_aggs')]).toEqual([[], 0])
  })

  it('with no assignments has none to wait for: a lens folds nothing, and reads no pricing', async () => {
    const m = await cold()
    const { iv } = await seed('')
    const sql0 = sqlLog.length
    expect(await m.ownerAssignments(iv, S2)).toEqual([])
    expect(sqlLog.slice(sql0)).toEqual(['head', 'ledger'])
    // The totals fold the roots (each bucket's attribution), which the fill prices with or without assignments.
    expect(await pending(m.ownerTotals(iv, S2))).toEqual({ scan: S2, index: 'pq:iv:g2:s', why: 'pending', count: 0, prefixes: [], roots: true })
    expect(await fillAll(m, iv, 64)).toEqual([[0, 0]])
    expect(fold(await m.ownerTotals(iv, S2))).toEqual(await perScanTotals((await seed('')).perScan, S2))
  })

  it('as a response: 503 + Retry-After while a fill is pending, 409 for a scan none will price, with the body', async () => {
    const m = await cold()
    const { ivRefusal } = await import('./index')
    const { perScan, iv } = await seed()
    const answers = []
    for (const env of [iv, perScan]) {
      const r = ivRefusal(await m.ownerTotals(env, S2).then(() => null, e => e))!
      answers.push([r.status, r.headers.get('retry-after'), r.headers.get('cache-control'), await r.json()])
    }
    expect(answers).toEqual([
      [503, '60', 'no-store', {
        error: `owner pricing is pending for scan ${S2}: 3 assigned prefixes and the store roots not priced through it yet (filled by the scan job's owner-prices step, or by the assignment that added a prefix)`,
        code: 'owner-pricing-pending',
        pricing: { scan: S2, index: 'pq:iv:g2:s', why: 'pending', count: 3, prefixes: ['bk', 'bk/m', 'bk/nest'], roots: true },
      }],
      [409, null, 'no-store', {
        error: `owner totals aren't available for scan ${S2}: the interval store doesn't hold it, and owner pricing is filled per store generation`,
        code: 'owner-pricing-pending',
        pricing: { scan: S2, index: 'd1:g', why: 'unheld', count: 3, prefixes: ['bk', 'bk/m', 'bk/nest'], roots: true },
      }],
    ])
  })
})

describe('a new assignment between scans', () => {
  const env = (iv: Env) => ({ ...iv, STORE_SCHEME: 'gs://', STORE_BUCKETS: 'bk', DEV_EMAIL: 'ann@example.test' }) as Env
  const post = async (e: Env, actions: unknown) => {
    const { onRequest } = await import('../api/actions')
    const r = await onRequest({ request: new Request('http://localhost/api/actions', { method: 'POST', body: JSON.stringify(actions) }), env: e } as never)
    return [r.status, await r.json()]
  }

  it('is priced by the write, over every scan in one read, so the next read is ready', async () => {
    const filled = await cold()
    const { perScan, iv } = await seed()
    await fillAll(filled, iv, 64)
    await cold()
    const r20 = r2Reads.length
    expect(await post(env(iv), { pattern: 'gs://bk/small/', owner: 'dan', allow_missing: true })).toEqual([200, { ok: true, count: 1, ts: expect.any(Number), pricing: { priced: 1, pending: 0 } }])
    // The one history read: the generation's slice sort.
    expect([...new Set(dataReads(r20))]).toEqual(['interval-store/g2/served/slices.parquet'])
    expect((await hist(iv)).map(r => [r.depth, r.path, r.hz])).toEqual([[0, '', T3], [1, 'bk', T3], [2, 'bk/m', T3], [2, 'bk/nest', T3], [2, 'bk/small', T3]])
    const m = await cold()
    const n = r2Reads.length
    for (const date of [S1, S2, S3]) expect(fold(await m.ownerTotals(iv, date))).toEqual(await perScanTotals(perScan, date))
    // (the oracle reads the per-scan stores, not `INDEX_R2`: the folds read no row data)
    expect(dataReads(n)).toEqual([])
  })

  it('an owner change on a priced prefix prices nothing', async () => {
    const filled = await cold()
    const { iv } = await seed()
    await fillAll(filled, iv, 64)
    const before = await hist(iv)
    await cold()
    const r20 = r2Reads.length
    expect(await post(env(iv), { pattern: 'gs://bk/m/', owner: 'carol', allow_missing: true })).toEqual([200, { ok: true, count: 1, ts: expect.any(Number), pricing: { priced: 0, pending: 0 } }])
    expect([await hist(iv), dataReads(r20)]).toEqual([before, []])
  })

  it('more new prefixes than one read prices: the first 64 priced, the rest pending, and reads say so', async () => {
    const filled = await cold()
    const { iv } = await seed()
    await fillAll(filled, iv, 64)
    await cold()
    const actions = Array.from({ length: 70 }, (_, i) => ({ pattern: `gs://bk/new/d${String(i).padStart(2, '0')}/`, owner: 'dan', allow_missing: true }))
    expect(await post(env(iv), actions)).toEqual([200, { ok: true, count: 70, ts: expect.any(Number), pricing: { priced: 64, pending: 6 } }])
    const m = await cold()
    expect(await pending(m.ownerTotals(iv, S2))).toEqual({
      scan: S2, index: 'pq:iv:g2:s', why: 'pending', count: 6, prefixes: [64, 65, 66, 67, 68, 69].map(i => `bk/new/d${i}`), roots: false,
    })
    // The next fill (the scan job's) prices them.
    expect(await fillAll(m, iv, 64)).toEqual([[6, 0]])
    expect((await m.ownerTotals(iv, S2)).assignments.length).toBe(73)
  })
})

describe('a generation move', () => {
  it('carries the pricing to the new generation: exactly what a fill of it writes', async () => {
    // `g2` and `g5` hold the same scans (one base; a base and a run), as a generation and its compaction do.
    const m = await cold()
    const { iv } = await seed()
    await fillAll(m, iv, 64)
    expect(await m.carryOwnerHist(iv, 'g2', 'g5')).toEqual({ from: 'pq:iv:g2:s', index: 'pq:iv:g5:s', hz: T3, carried: 4, remaining: 0, roots: false })
    const rows = (idx: string, all: Awaited<ReturnType<typeof hist>>) => all.filter(r => r.idx === idx).map(({ idx: _, ...r }) => r)
    const carried = rows('pq:iv:g5:s', await hist(iv))
    const m5 = await cold()
    const fresh = await seed(LEDGER, 'g5')
    await fillAll(m5, fresh.iv, 64)
    expect(carried).toEqual(rows('pq:iv:g5:s', await hist(fresh.iv)))
    // …and the old generation's rows stand.
    expect(rows('pq:iv:g2:s', await hist(iv))).toEqual(carried)
    // A read of the new generation is ready at once, with no fill.
    const read = await cold()
    const sql0 = sqlLog.length
    expect(fold(await read.ownerTotals({ ...iv, INTERVAL_STORE_GEN: 'g5' } as Env, S3))).toEqual(await perScanTotals(fresh.perScan, S3))
    expect(sqlLog.slice(sql0).filter(k => k.startsWith('write'))).toEqual([])
  })

  it('says what the new generation still lacks when the old one was not filled', async () => {
    const m = await cold()
    const { iv } = await seed()
    await m.fillOwnerHist(iv, 2)
    expect(await m.carryOwnerHist(iv, 'g2', 'g2.c001')).toEqual({ from: 'pq:iv:g2:s', index: 'pq:iv:g2.c001:s', hz: T3, carried: 3, remaining: 1, roots: false })
  })

  it('keeps a row the new generation prices further, and is idempotent', async () => {
    const m = await cold()
    const { iv } = await seed()
    await fillAll(m, iv, 64)
    await m.carryOwnerHist(iv, 'g2', 'g2.c001')
    await iv.DB!.prepare("UPDATE owner_prefix_hist SET hz = hz + 60, versions = '[]' WHERE idx = 'pq:iv:g2.c001:s' AND path = 'bk/m'").run()
    expect(await m.carryOwnerHist(iv, 'g2', 'g2.c001')).toEqual({ from: 'pq:iv:g2:s', index: 'pq:iv:g2.c001:s', hz: T3, carried: 4, remaining: 0, roots: false })
    expect((await hist(iv)).filter(r => r.idx === 'pq:iv:g2.c001:s').map(r => [r.path, r.hz, r.path === 'bk/m' ? r.versions : ''])).toEqual([['', T3, ''], ['bk', T3, ''], ['bk/m', T3 + 60, '[]'], ['bk/nest', T3, '']])
  })

  it('with nothing priced (no assignments, never filled) carries nothing and blocks nothing', async () => {
    const m = await cold()
    const { iv } = await seed('')
    expect(await m.carryOwnerHist(iv, 'g2', 'g2.c001')).toEqual({ from: 'pq:iv:g2:s', index: 'pq:iv:g2.c001:s', hz: T3, carried: 0, remaining: 0, roots: true })
  })
})

describe('pruning', () => {
  it('drops the rows of generations the store no longer holds, and keeps every held generation\'s (a dev stack\'s too)', async () => {
    const m = await cold()
    const { iv } = await seed()
    await fillAll(m, iv, 64)
    await fillAll(m, iv, 64, { gen: 'g5' })
    await m.carryOwnerHist(iv, 'g2', 'gone.c001')
    await iv.DB!.prepare("INSERT INTO owner_prefix_hist (idx, depth, path, hz, versions, updated_ts) VALUES ('pq:iv:2026-10-09b:s', 2, 'bk/m', 1, '[]', 1)").run()
    expect(await m.pruneOwnerHist(iv)).toEqual({
      kept: ['pq:iv:g2:s', 'pq:iv:g5:s'],
      dropped: [{ index: 'pq:iv:2026-10-09b:s', rows: 1 }, { index: 'pq:iv:gone.c001:s', rows: 4 }],
    })
    expect([...new Set((await hist(iv)).map(r => r.idx))]).toEqual(['pq:iv:g2:s', 'pq:iv:g5:s'])
    expect(await m.pruneOwnerHist(iv)).toEqual({ kept: ['pq:iv:g2:s', 'pq:iv:g5:s'], dropped: [] })
  })
})

describe('POST /api/owner-prices', () => {
  const call = async (env: Env, token: string | null, query = '', origin = 'https://site.example.test') => {
    const { onRequestPost } = await import('../api/owner-prices')
    const r = await onRequestPost({ request: new Request(`${origin}/api/owner-prices${query}`, { method: 'POST', headers: token ? { authorization: `Bearer ${token}` } : {} }), env } as never)
    const body = await r.json() as Record<string, unknown>
    delete body.ms
    return { status: r.status, body }
  }
  const mint = (env: Env, name: string, scopes: string[]) => gateFor(env as never)!.mint({ name, scopes, createdBy: 'test', expiresAt: null, maxRedeems: null })

  it('is the named grant\'s alone: 404 unset, 401 without a token, 403 for an admin or another grant, 200 for the grant', async () => {
    await cold()
    const { iv } = await seed()
    const job = await mint(iv, 'scan job', ['gcs:read'])
    const admin = await mint(iv, 'ops', ['gcs', 'admin'])
    const reader = await mint(iv, 'share link', ['gcs:read'])
    const gone = { status: 404, body: { error: 'no owner-prices grant on this deployment' } }
    expect([await call(iv, job.token), await call(iv, admin.token), await call(iv, null)]).toEqual([gone, gone, gone])
    const env = { ...iv, OWNER_PRICES_GRANT: job.grant.id } as Env
    const forbidden = { status: 403, body: { error: 'forbidden' } }
    expect([
      await call(env, null),
      await call(env, admin.token),
      await call(env, reader.token),
      // (a localhost request is the dev identity: a session with every scope, admin included)
      await call(env, null, '', 'http://localhost'),
      await hist(env),
    ]).toEqual([{ status: 401, body: { error: 'unauthenticated' } }, forbidden, forbidden, forbidden, []])
    expect(await call(env, job.token)).toEqual({ status: 200, body: { index: 'pq:iv:g2:s', hz: T3, prefixes: 3, priced: 3, remaining: 0, groups: expect.any(Number) } })
  })

  it('fills in bounded chunks until nothing remains, then answers the same with nothing to price', async () => {
    await cold()
    const { iv } = await seed()
    const job = await mint(iv, 'scan job', ['gcs:read'])
    const env = { ...iv, OWNER_PRICES_GRANT: job.grant.id } as Env
    const calls = []
    for (let i = 0; i < 3; i++) calls.push((await call(env, job.token, '?n=2')).body)
    expect(calls).toEqual([
      { index: 'pq:iv:g2:s', hz: T3, prefixes: 3, priced: 2, remaining: 1, groups: expect.any(Number) },
      { index: 'pq:iv:g2:s', hz: T3, prefixes: 3, priced: 1, remaining: 0, groups: expect.any(Number) },
      { index: 'pq:iv:g2:s', hz: T3, prefixes: 3, priced: 0, remaining: 0, groups: 0 },
    ])
  })

  it('409s a scan the store does not hold yet, carries a generation, prunes, and refuses a bad generation', async () => {
    await cold()
    const { iv } = await seed()
    const job = await mint(iv, 'scan job', ['gcs:read'])
    const env = { ...iv, OWNER_PRICES_GRANT: job.grant.id } as Env
    expect([
      await call(env, job.token, '?scan=2026-10-01T0000'),
      await call(env, job.token, `?scan=${S3}`),
      await call(env, job.token, '?carry=g2.c001&from=g2'),
      await call(env, job.token, '?prune=1'),
      await call(env, job.token, '?gen=../x'),
      await call(env, job.token, '?carry=g2.c001'),
    ]).toEqual([
      { status: 409, body: { error: "the interval store doesn't hold 2026-10-01T0000 yet", behind: '2026-10-01T0000' } },
      { status: 200, body: { index: 'pq:iv:g2:s', hz: T3, prefixes: 3, priced: 3, remaining: 0, groups: expect.any(Number) } },
      { status: 200, body: { from: 'pq:iv:g2:s', index: 'pq:iv:g2.c001:s', hz: T3, carried: 4, remaining: 0, roots: false } },
      { status: 200, body: { kept: ['pq:iv:g2:s'], dropped: [{ index: 'pq:iv:g2.c001:s', rows: 4 }] } },
      { status: 400, body: { error: 'bad generation' } },
      { status: 400, body: { error: 'carry needs from (the generation compacted) and carry (its successor)' } },
    ])
  })

  it('has nothing to price on a deployment without a ledger, or without an interval store', async () => {
    await cold()
    const { perScan, iv, raw } = await seed()
    const job = await mint(iv, 'scan job', ['gcs:read'])
    expect(await call({ ...perScan, OWNER_PRICES_GRANT: job.grant.id } as Env, job.token)).toEqual({ status: 409, body: { error: 'no interval store serves this deployment' } })
    withoutLedger(raw)
    await cold()
    expect(await call({ ...iv, OWNER_PRICES_GRANT: job.grant.id } as Env, job.token, '?carry=g2.c001&from=g2')).toEqual({ status: 200, body: { ledger: false, priced: 0, remaining: 0 } })
  })
})

describe('a scoped series', () => {
  const series = async (env: Env, query: string) => {
    const { seriesRead } = await import('../api/series')
    const r = await seriesRead({ request: new Request(`https://site.example.test/api/series?${query}`), env: { ...env, STORE_BUCKET: 'b', STORE_SCHEME: 'gs://' } } as never, async () => null)
    return { status: r.status, cache: r.headers.get('cache-control'), body: await r.json() as { points: { date: string; b: number; o: number }[]; pricing?: unknown } }
  }

  it('leaves out the scans whose pricing is not filled and names them, instead of pricing in the request', async () => {
    const filled = await cold()
    const { iv } = await seed()
    await fillAll(filled, iv, 64)
    await cold()
    const whole = await series(iv, 'path=&lens=user:carol')
    expect([whole.status, whole.body.points.map(p => p.date), whole.body.pricing]).toEqual([200, [S1, S2, S3], undefined])
    // As after an append: S3 not priced yet.
    await iv.DB!.prepare('UPDATE owner_prefix_hist SET hz = ?').bind(T2).run()
    await cold()
    const sql0 = sqlLog.length
    const got = await series(iv, 'path=&o=owned')
    expect([got.status, got.cache, got.body.points.map(p => p.date), got.body.pricing]).toEqual([
      200, 'no-store', [S1, S2],
      { pending: [S3], unheld: [], count: 3, prefixes: ['bk', 'bk/m', 'bk/nest'], roots: true },
    ])
    expect(sqlLog.slice(sql0).filter(k => k.startsWith('write'))).toEqual([])
    // …and the first two points are the filled series' own.
    const pooled = await (async () => { const m = await cold(); await fillAll(m, iv, 64); await cold(); return series(iv, 'path=&o=owned') })()
    expect([got.body.points, pooled.body.pricing]).toEqual([pooled.body.points.slice(0, 2), undefined])
  })

  it('with nothing priced at all is every scan pending, not an error and not a zero line', async () => {
    await cold()
    const { iv } = await seed()
    const got = await series(iv, 'path=&lens=user:carol')
    expect([got.status, got.cache, got.body.points, got.body.pricing]).toEqual([
      200, 'no-store', [],
      { pending: [S1, S2, S3], unheld: [], count: 3, prefixes: ['bk', 'bk/m', 'bk/nest'], roots: true },
    ])
    expect([await hist(iv), await count(iv, 'owner_prefix_aggs')]).toEqual([[], 0])
  })
})
