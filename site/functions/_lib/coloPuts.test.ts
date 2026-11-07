import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { COLO_PUT_SLOTS, coloPutsSettled, gzipChunks, jsonChunks, Slots } from './index'
import { clearIsolate } from './shared'
import { fixture } from './testStore'
import { sqliteD1 } from './testD1'
import { buildView } from './view'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))
const gunzip = async (buf: ArrayBuffer): Promise<string> => new Response(new Response(buf).body!.pipeThrough(new DecompressionStream('gzip'))).text()

async function storeEnv(): Promise<Env> {
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { existsSync(path: string): boolean; readFileSync(path: string): Uint8Array }
  const bucket = {
    get: async (key: string, o?: { range?: { offset: number; length: number } }) => {
      const file = fixture(`iv/${key}`)
      if (!fs.existsSync(file)) return null
      const buf = fs.readFileSync(file)
      const { offset = 0, length = buf.byteLength - offset } = o?.range ?? {}
      const part = buf.slice(offset, offset + length)
      return {
        size: buf.byteLength,
        arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength),
        json: async () => JSON.parse(new TextDecoder().decode(part)),
      }
    },
  } as unknown as R2Bucket
  const { db } = await sqliteD1()
  return { DB: db, PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g1', INDEX_R2: bucket } as Env
}

describe('a colo copy, serialized in pieces', () => {
  it('is exactly `JSON.stringify` of the doc, whatever its values and lengths', async () => {
    const n = 5000
    const doc = {
      cols: ['path', 'depth', 'size', 'usr', 'kind', 'flag'],
      v: [
        Array.from({ length: n }, (_, i) => `b/dir "${i}"/é \\x/${'z'.repeat(i % 7)}`),
        Array.from({ length: n }, (_, i) => i % 9),
        Array.from({ length: n }, (_, i) => i * 1e9 + 0.5),
        Array.from({ length: n }, (_, i) => (i % 3 ? `u${i % 5}` : null)),
        [],
        Array.from({ length: n }, (_, i) => i % 2 === 0),
      ],
    }
    expect([...jsonChunks(doc)].join('')).toBe(JSON.stringify(doc))
    expect([...jsonChunks({ a: [], b: [[]], c: [[1], []] })].join('')).toBe(JSON.stringify({ a: [], b: [[]], c: [[1], []] }))
    expect([...jsonChunks({})].join('')).toBe('{}')
    expect(await gunzip(await gzipChunks(jsonChunks(doc)))).toBe(JSON.stringify(doc))
  })

  it('never holds more than one slice of a column at once', () => {
    const doc = { cols: ['path'], v: [Array.from({ length: 10_000 }, (_, i) => `p/${i}`)] }
    // Each piece is one `[` or `,` and up to 2048 elements (the longest here: the last full slice), or punctuation.
    const pieces = [...jsonChunks(doc)].map(c => c.length)
    expect(Math.max(...pieces)).toBe(JSON.stringify(doc.v[0].slice(6144, 8192)).length - 1)
    expect(pieces.join('').length).toBeLessThan(JSON.stringify(doc).length)
  })
})

describe('`Slots`', () => {
  it('runs `slots` jobs at once, queues `cap` more, and makes the next caller wait for a place', async () => {
    const q = new Slots('test', 2, 3)
    const release = new Map<number, () => void>()
    const started: number[] = []
    const finished: number[] = []
    const accepted: number[] = []
    const waits: number[] = []
    const job = (i: number) => async () => { started.push(i); await new Promise<void>(r => release.set(i, r)); finished.push(i) }
    const adds = Array.from({ length: 7 }, (_, i) => q.add(job(i), () => waits.push(i)).then(x => { accepted.push(i); return x }))
    await sleep(5)
    expect([started, accepted, waits]).toEqual([[0, 1], [0, 1, 2, 3, 4], [5, 6]])
    release.get(0)!()
    await sleep(5)
    // 0's slot goes to 2 (the oldest queued), and its queue place to 5 (the oldest waiting).
    expect([started, accepted, finished]).toEqual([[0, 1, 2], [0, 1, 2, 3, 4, 5], [0]])
    for (let i = 1; i < 7; i++) { await sleep(5); release.get(i)!() }
    await Promise.all((await Promise.all(adds)).map(x => x.done))
    expect([started, finished]).toEqual([[0, 1, 2, 3, 4, 5, 6], [0, 1, 2, 3, 4, 5, 6]])
  })

  it('frees its waiting callers when the isolate is cleared', async () => {
    const q = new Slots('test', 1, 0)
    let open!: () => void
    await q.add(() => new Promise<void>(r => { open = r }))
    let got = false
    const waiting = q.add(async () => {}).then(() => { got = true })
    await sleep(5)
    expect(got).toBe(false)
    clearIsolate()
    await waiting
    expect(got).toBe(true)
    open()
  })
})

describe('colo copies of a read\'s decoded groups', () => {
  beforeEach(() => {
    clearIsolate()
    ;(globalThis as unknown as { caches: unknown }).caches = { default: {} }
  })

  it(`are written ${COLO_PUT_SLOTS} at a time, every one landing`, async () => {
    const env = await storeEnv()
    const landed = new Map<string, ArrayBuffer>()
    const held: (() => void)[] = []
    let inFlight = 0
    let most = 0
    ;(globalThis as unknown as { caches: { default: unknown } }).caches.default = {
      match: async () => undefined,
      put: async (k: Request, r: Response) => {
        if (!k.url.includes('rg=')) return
        inFlight++
        most = Math.max(most, inFlight)
        const buf = await r.arrayBuffer()
        await new Promise<void>(res => held.push(res))
        landed.set(k.url, buf)
        inFlight--
      },
    }
    const n: Record<string, number> = {}
    const view = await buildView(env, { date: '2026-08-03', path: '', w: 8, h: 6, minArea: 12, atten: 2, ivPlanRows: 0, trace: name => { n[name] = (n[name] ?? 0) + 1 } })
    await sleep(20)
    expect([most, inFlight, held.length]).toEqual([COLO_PUT_SLOTS, COLO_PUT_SLOTS, COLO_PUT_SLOTS])
    // Released one at a time: each frees its slot for the next queued copy, never more than the slots at once.
    while (held.length) {
      held.shift()!()
      await sleep(5)
      expect(inFlight).toBeLessThanOrEqual(COLO_PUT_SLOTS)
    }
    await coloPutsSettled()
    expect(landed.size).toBeGreaterThan(COLO_PUT_SLOTS)
    for (const buf of landed.values()) expect(Object.keys(JSON.parse(await gunzip(buf)))).toEqual(['cols', 'v'])
    // The same view from the copies alone: nothing decoded again, the same tree.
    clearIsolate()
    const copies = new Map(landed)
    ;(globalThis as unknown as { caches: { default: unknown } }).caches.default = {
      match: async (k: Request) => (copies.has(k.url) ? new Response(copies.get(k.url)!) : undefined),
      put: async () => {},
    }
    const m: Record<string, number> = {}
    const again = await buildView(env, { date: '2026-08-03', path: '', w: 8, h: 6, minArea: 12, atten: 2, ivPlanRows: 0, trace: name => { m[name] = (m[name] ?? 0) + 1 } })
    expect(again.tree).toEqual(view.tree)
    expect([m.gcolo, m.gput ?? 0]).toEqual([landed.size, 0])
  })
})
