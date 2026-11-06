import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import type { Ask } from './index'
import { sqliteD1, withLedger } from './testD1'
import { fixture } from './testStore'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

const S2 = '2026-09-30T0008'
const LEDGER = `
  INSERT INTO actions (id, actor, ts, scan, pattern, set_owner, owner) VALUES
    (1, 'ops', 1, '${S2}', 'gs://bk/m/', 1, 'alice'), (2, 'ops', 2, '${S2}', 'gs://bk/', 1, 'bob'), (3, 'ops', 3, '${S2}', 'gs://bk/nest/', 1, 'carol');
  INSERT INTO owner_prefixes (action_id, prefix, owner, ts) VALUES (1, 'gs://bk/m/', 'alice', 1), (2, 'gs://bk/', 'bob', 2), (3, 'gs://bk/nest/', 'carol', 3);
`

/** Ranged gets of the store's data sorts (not their `.groups` footers). */
const dataReads: string[] = []

function r2(): R2Bucket {
  const fs = () => import(/* @vite-ignore */ 'node:fs' as string) as Promise<{ existsSync(p: string): boolean; readFileSync(p: string): Uint8Array; readdirSync(p: string, o: { recursive: true }): string[]; statSync(p: string): { isFile(): boolean } }>
  const get = async (key: string, o?: { range?: { offset: number; length: number } | { suffix: number } }) => {
    const { existsSync, readFileSync } = await fs()
    const file = fixture(`iv/${key}`)
    if (!existsSync(file)) return null
    const buf = readFileSync(file)
    const r = o?.range
    const { offset = 0, length = buf.byteLength - offset } = r && 'suffix' in r ? { offset: Math.max(0, buf.byteLength - r.suffix) } : r ?? {}
    if (r && !('suffix' in r) && key.endsWith('.parquet') && !key.endsWith('.groups.parquet')) dataReads.push(`${key}@${offset}+${length}`)
    const part = buf.slice(offset, offset + length)
    return { size: buf.byteLength, arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength), json: async () => JSON.parse(new TextDecoder().decode(part)) }
  }
  const list = async ({ prefix, cursor }: { prefix: string; cursor?: string }) => {
    const { existsSync, readdirSync, statSync } = await fs()
    const dir = prefix.slice(0, prefix.lastIndexOf('/') + 1)
    const root = fixture(`iv/${dir}`)
    const keys = existsSync(root) ? readdirSync(root, { recursive: true }).map(f => `${dir}${f}`).filter(k => k.startsWith(prefix) && statSync(fixture(`iv/${k}`)).isFile()).sort() : []
    return { objects: keys.map(key => ({ key })), truncated: false, cursor }
  }
  const head = async (key: string) => (await fs()).existsSync(fixture(`iv/${key}`)) ? { key } : null
  return { get, list, head } as unknown as R2Bucket
}

async function envOf(gen: string): Promise<Env> {
  const { db, raw } = await sqliteD1()
  withLedger(raw)
  raw.exec(LEDGER)
  return { DB: db, ROOT_LABEL: 'root', BASE_SCOPE: 'gcs', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', PATH_STORE: 'intervals', INTERVAL_STORE_GEN: gen, INDEX_R2: r2() } as Env
}

/** The colo: copies by URL, its calls counted. */
function colo() {
  const copies = new Map<string, { buf: ArrayBuffer; headers: [string, string][] }>()
  const calls = { match: [] as string[], put: [] as string[] }
  ;(globalThis as unknown as { caches: unknown }).caches = {
    default: {
      match: async (k: Request) => {
        calls.match.push(k.url)
        const c = copies.get(k.url)
        return c && new Response(c.buf.slice(0), { headers: c.headers })
      },
      put: async (k: Request, r: Response) => {
        calls.put.push(k.url)
        copies.set(k.url, { buf: await r.arrayBuffer(), headers: [...r.headers] })
      },
    },
  }
  return { copies, calls }
}

const priced = async (env: Env) => (await env.DB!.prepare('SELECT depth, path, hz, versions FROM owner_prefix_hist ORDER BY depth, path').all<{ depth: number; path: string; hz: number; versions: string }>()).results
  .map(r => ({ ...r, versions: JSON.parse(r.versions) }))

/** A new isolate: nothing in memory, the colo as it is. */
const isolate = async () => {
  vi.resetModules()
  const [totals, index, hist] = await Promise.all([import('./ownerTotals'), import('./index'), import('./ownerHist')])
  return { fillOwnerHist: totals.fillOwnerHist, index, hist }
}

const version = (vf: number, vt: number, b: number, o: number, us: Record<string, number>, uo: Record<string, number>) => [vf, vt, { b, o, us, uo, cb: {} }]
const [T1, T2, T3, END] = [1790726820, 1790726880, 1790726940, 4291747200]
const FULL = { us: { alice: 341297152, bob: 338636800, carol: 819200 }, uo: { alice: 1105, bob: 1103, carol: 4 } }
const M = { us: { alice: 3377152, bob: 716800 }, uo: { alice: 5, bob: 3 } }
/** The priced rows as `fields` (a column subset, read past the colo) wrote them, before reads went through it. */
const PRICED = [
  { depth: 0, path: '', hz: T3, versions: [] },
  { depth: 1, path: 'bk', hz: T3, versions: [version(T1, T2, 683620352, 4713, FULL.us, FULL.uo), version(T2, T3, 37229948, 8009, {}, {}), version(T3, END, 683620352, 4713, FULL.us, FULL.uo)] },
  { depth: 2, path: 'bk/m', hz: T3, versions: [version(T1, T2, 6961152, 2509, M.us, M.uo), version(T3, END, 6961152, 2509, M.us, M.uo)] },
  { depth: 2, path: 'bk/nest', hz: T3, versions: [version(T2, T3, 4456448, 5, { alice: 4456448 }, { alice: 5 })] },
]
const isGroup = (u: string) => u.includes('rg=')

describe.each([
  // Generation, the row groups pricing reads, the merged range reads they take, the rows they hold, the rows it keeps:
  // `g5` is `g2`'s scans as a base and a run.
  { gen: 'g2', groups: 3, ranges: 1, held: 768, kept: 16 },
  { gen: 'g5', groups: 4, ranges: 2, held: 1024, kept: 18 },
])('owner pricing on $gen', ({ gen, groups, ranges, held, kept }) => {
  beforeEach(() => { dataReads.length = 0 })
  afterEach(() => { vi.restoreAllMocks() })

  it('prices what a column-subset read prices', async () => {
    colo()
    const env = await envOf(gen)
    const { fillOwnerHist, index, hist } = await isolate()
    expect((await fillOwnerHist(env, 64))!.groups).toBe(groups)
    await index.coloPutsSettled()
    const rows = await priced(env)
    expect(rows).toEqual(PRICED)
    // The column-subset branch stays for other readers: pricing's answer, read through it now.
    const scoped = index.slices(env)
    const dates = await index.ivHeldDates(scoped)
    const open = await index.openInterval(index.asOfScans(scoped, dates), dates[dates.length - 1], 'path')
    const historyRange: [number, number] = [T1, T3]
    const h = { ...open!, historyRange, runs: open!.runs?.map(r => ({ ...r, historyRange })) }
    const want = new Map([['bk/m', 2], ['bk/nest', 2]])
    const asks: Ask[] = [...want].map(([path, depth]) => ({ depth, path }))
    asks.push({ depth: 1, under: '' })
    const subset = await index.readAsks(h, asks, r => want.get(r.path) === r.depth || r.depth === 1, {
      versions: true,
      maxGroups: 400,
      columns: index.columnsFor(open!, ['path', 'depth', 'usr', 'size', 'n_files', 'cls2', 'cls3', 'cls4']),
    })
    const vs = hist.versionsOf(subset.rows)
    expect(rows.slice(1).map(r => r.versions)).toEqual(rows.slice(1).map(r => vs.get(`${r.depth}:${r.path}`)))
  })

  it('reads no row group from the store when the colo holds them: a repeat is warm', async () => {
    const { calls } = colo()
    const env = await envOf(gen)
    const first = await isolate()
    await first.fillOwnerHist(env, 64)
    await first.index.coloPutsSettled()
    // Cold: one `match` and one `put` per group, neighbours sharing a range read.
    expect([dataReads.length, calls.match.filter(isGroup).length, calls.put.filter(isGroup).length]).toEqual([ranges, groups, groups])
    dataReads.length = 0
    calls.match.length = calls.put.length = 0
    await env.DB!.prepare('DELETE FROM owner_prefix_hist').run()
    const again = await isolate()
    await again.fillOwnerHist(env, 64)
    await again.index.coloPutsSettled()
    // Warm in the colo, cold in the isolate: one `match` per group, nothing else.
    expect([dataReads, calls.match.filter(isGroup).length, calls.put.filter(isGroup).length]).toEqual([[], groups, 0])
    expect(await priced(env)).toEqual(PRICED)
  })

  it('shapes only the rows it keeps, cold and warm', async () => {
    colo()
    const env = await envOf(gen)
    for (const pass of ['cold', 'warm']) {
      const { fillOwnerHist, index } = await isolate()
      const all = vi.spyOn(index.IvGroup.prototype, 'all')
      const pick = vi.spyOn(index.IvGroup.prototype, 'pick')
      await env.DB!.prepare('DELETE FROM owner_prefix_hist').run()
      await fillOwnerHist(env, 64)
      await index.coloPutsSettled()
      const sum = (l: number[]) => l.reduce((a, b) => a + b, 0)
      expect([
        pass,
        all.mock.calls.length,
        sum(pick.mock.contexts.map(g => (g as { length: number }).length)),
        sum(pick.mock.results.map(r => (r.value as unknown[]).length)),
      ]).toEqual([pass, 0, held, kept])
    }
  })

  it('takes each colo copy as it arrives: none waits in memory for the slowest', async () => {
    const { copies } = colo()
    const env = await envOf(gen)
    const first = await isolate()
    await first.fillOwnerHist(env, 64)
    await first.index.coloPutsSettled()
    await env.DB!.prepare('DELETE FROM owner_prefix_hist').run()
    const { fillOwnerHist, index } = await isolate()
    const pick = vi.spyOn(index.IvGroup.prototype, 'pick')
    const slow = [...copies.keys()].filter(isGroup)[0]
    let release!: () => void
    const held1 = new Promise<void>(r => { release = r })
    const c = (globalThis as unknown as { caches: { default: { match: (k: Request) => Promise<Response | undefined> } } }).caches.default
    const match = c.match
    c.match = async (k: Request) => {
      if (k.url === slow) await held1
      return match(k)
    }
    const read = fillOwnerHist(env, 64)
    await vi.waitFor(() => expect(pick.mock.calls.length).toBe(groups - 1))
    release()
    await read
    expect(pick.mock.calls.length).toBe(groups)
    expect(await priced(env)).toEqual(PRICED)
  })
})
