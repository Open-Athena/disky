import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { warmSubtree } from '../api/subtree'
import { ivRefusal } from './index'
import { chargedElsewhere, chargedRead, Grant, planCharge, READ_CAP, READ_REQUEST_HEAP, READ_ROW_HEAP, READ_SMALL_ROWS, ReadGate, readGate, ReadsBusy, type Ticket, withReads } from './readCap'
import { clearIsolate, REQ, Req } from './shared'
import { fixture } from './testStore'
import { sqliteD1 } from './testD1'
import { buildDiff, buildView } from './view'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))
const OPTS = { bytes: 100, waitMs: 2_000, maxWaiters: 4, pollMs: 1, staleMs: 200, beatMs: 5, leaseMs: 200, holdMs: 10_000 }
const flush = () => sleep(40)

/** A log of what got in, in order; each `enter` holds its ticket until the test releases it. */
function logged(gate: ReadGate) {
  const log: string[] = []
  const held = new Map<string, Ticket>()
  const enter = (name: string, bytes: number, signal?: AbortSignal) => {
    const p = gate.admit(bytes, signal).then(t => { log.push(name); held.set(name, t); return t })
    p.catch(() => {})
    return p
  }
  return { log, held, enter }
}

describe('a gate', () => {
  it('admits in arrival order: one that would fit waits behind the one that wouldn\'t', async () => {
    const { log, held, enter } = logged(new ReadGate(OPTS))
    await enter('a', 60)
    const b = enter('b', 60)
    const c = enter('c', 10)
    await flush()
    expect(log).toEqual(['a'])
    held.get('a')!.release()
    await b
    await flush()
    // `c` fits beside `b`, in its turn.
    await c
    expect(log).toEqual(['a', 'b', 'c'])
  })

  it('runs a plan bigger than the cap alone, once nothing else runs, and nothing passes it', async () => {
    const gate = new ReadGate(OPTS)
    const { log, held, enter } = logged(gate)
    await enter('small', 10)
    const big = enter('big', 500)
    const after = enter('after', 10)
    await flush()
    expect(log).toEqual(['small'])
    held.get('small')!.release()
    await big
    await flush()
    // Alone: `after` would fit the cap by itself, and waits for `big`.
    expect(log).toEqual(['small', 'big'])
    expect(gate.bytes()).toBe(500)
    held.get('big')!.release()
    await after
    expect(log).toEqual(['small', 'big', 'after'])
  })

  it('counts charges made elsewhere against the cap, and a plan runs alone only with none of them', async () => {
    const gate = new ReadGate(OPTS)
    let level = 50
    gate.held = () => level
    const { log, enter } = logged(gate)
    const p = enter('a', 60)
    await flush()
    expect(log).toEqual([])
    level = 0
    await p
    expect(log).toEqual(['a'])
  })

  it('refuses a wait past its bound, as a retryable busy that leaves the line', async () => {
    const gate = new ReadGate({ ...OPTS, waitMs: 20 })
    const { held, enter } = logged(gate)
    await enter('a', 90)
    await expect(enter('b', 90)).rejects.toThrow(new ReadsBusy('no room for 0 MiB in 20 ms'))
    expect(gate.waiting).toBe(0)
    held.get('a')!.release()
    expect(gate.bytes()).toBe(0)
  })

  it('refuses at once past its waiters\' bound', async () => {
    const gate = new ReadGate({ ...OPTS, maxWaiters: 1 })
    const { log, held, enter } = logged(gate)
    await enter('a', 90)
    const b = enter('b', 90)
    const t0 = Date.now()
    await expect(enter('c', 1)).rejects.toThrow(new ReadsBusy('1 reads waiting'))
    expect(Date.now() - t0).toBeLessThan(OPTS.waitMs / 2)
    held.get('a')!.release()
    await b
    expect(log).toEqual(['a', 'b'])
  })

  it('gives a charge back when its client disconnects', async () => {
    const gate = new ReadGate(OPTS)
    const { log, enter } = logged(gate)
    const ctl = new AbortController()
    await enter('a', 90, ctl.signal)
    const b = enter('b', 90)
    await flush()
    expect(log).toEqual(['a'])
    ctl.abort()
    await b
    expect(log).toEqual(['a', 'b'])
    expect(gate.bytes()).toBe(90)
  })

  it('drops a waiter whose client disconnects, and the line moves past it', async () => {
    const gate = new ReadGate(OPTS)
    const { log, held, enter } = logged(gate)
    const ctl = new AbortController()
    await enter('a', 90)
    const gone = enter('gone', 90, ctl.signal)
    const c = enter('c', 90)
    await flush()
    ctl.abort()
    await expect(gone).rejects.toThrow(new ReadsBusy('client gone'))
    held.get('a')!.release()
    await c
    expect(log).toEqual(['a', 'c'])
  })

  it('takes back the charge of a request whose timers stopped (frozen, no abort)', async () => {
    const gate = new ReadGate(OPTS)
    const { log, enter } = logged(gate)
    // The holder's heartbeat never fires: what a cancelled request's timers do.
    const si = globalThis.setInterval
    globalThis.setInterval = (() => 0) as unknown as typeof setInterval
    await enter('frozen', 90)
    globalThis.setInterval = si
    const t0 = Date.now()
    await enter('b', 90)
    expect(log).toEqual(['frozen', 'b'])
    expect(Date.now() - t0).toBeGreaterThanOrEqual(OPTS.leaseMs - 5)
    expect(gate.bytes()).toBe(90)
  })

  it('keeps the charge of a live holder past its lease, by its heartbeat', async () => {
    const gate = new ReadGate(OPTS)
    const { log, held, enter } = logged(gate)
    await enter('alive', 90)
    const b = enter('b', 90)
    await sleep(OPTS.leaseMs * 2)
    expect(log).toEqual(['alive'])
    held.get('alive')!.release()
    await b
    expect(log).toEqual(['alive', 'b'])
  })

  it('drops a waiter whose timers stopped, and the line moves past it', async () => {
    const gate = new ReadGate(OPTS)
    const { log, held, enter } = logged(gate)
    await enter('a', 90)
    const st = globalThis.setTimeout
    globalThis.setTimeout = (() => 0) as unknown as typeof setTimeout
    void enter('frozen', 90)
    globalThis.setTimeout = st
    const c = enter('c', 90)
    held.get('a')!.release()
    await c
    expect(log).toEqual(['a', 'c'])
    expect(gate.waiting).toBe(0)
  })
})

describe('a request\'s charge', () => {
  it('is one: a request that holds never waits again, so two can\'t each hold part of the cap and wait for the other', async () => {
    const gate = new ReadGate(OPTS)
    const log: string[] = []
    const a = new Grant(gate)
    const b = new Grant(gate)
    await a.admit(60)
    log.push('a holds')
    const bIn = b.admit(60).then(t => { log.push('b holds'); return t })
    await flush()
    // Its later reads, with `b` waiting for room: past the cap by what the first asked, they run under the first.
    await a.admit(60)
    log.push('a read 2')
    await a.admit(1000)
    log.push('a read 3')
    expect(gate.bytes()).toBe(60)
    a.release()
    await bIn
    expect(log).toEqual(['a holds', 'a read 2', 'a read 3', 'b holds'])
    b.release()
    await flush()
    expect(gate.bytes()).toBe(0)
  })

  it('is given back when the wrapped answer ends, thrown or not', async () => {
    const gate = new ReadGate(OPTS)
    const env = { [REQ]: new Req() }
    await withReads(env, async () => {
      await env[REQ].reads!.admit(50)
      expect(gate.bytes()).toBe(50)
    }, { gate })
    await flush()
    expect(gate.bytes()).toBe(0)
    await expect(withReads(env, async () => {
      await env[REQ].reads!.admit(50)
      throw new Error('boom')
    }, { gate })).rejects.toThrow(new Error('boom'))
    await flush()
    expect(gate.bytes()).toBe(0)
    expect(env[REQ].reads).toBeUndefined()
  })

  it('survives its client\'s disconnect when it\'s background work', async () => {
    const gate = new ReadGate(OPTS)
    const ctl = new AbortController()
    const env = { [REQ]: new Req(ctl.signal) }
    await withReads(env, async () => {
      await env[REQ].reads!.admit(50)
      ctl.abort()
      await flush()
      expect(gate.bytes()).toBe(50)
    }, { gate, background: true })
    await flush()
    expect(gate.bytes()).toBe(0)
  })

  it('never waits when it holds a charge elsewhere (a level read\'s)', async () => {
    const full = await readGate.admit(READ_CAP.bytes)
    const env = { [REQ]: new Req() }
    chargedElsewhere(env)
    expect(await chargedRead(env, [{ key: 'a', rows: READ_SMALL_ROWS + 1 }], async () => 'read')).toBe('read')
    expect(readGate.bytes()).toBe(READ_CAP.bytes)
    full.release()
  })

  it('without a wrapper, is one read\'s own, released as it ends: nothing is held while the next waits', async () => {
    const gate = new ReadGate({ ...OPTS, bytes: READ_REQUEST_HEAP * 1.5 })
    const env = { [REQ]: new Req() }
    const seen: number[] = []
    const rows = READ_SMALL_ROWS + 1
    const read = () => chargedRead(env, [{ key: 'a', rows }], async () => { seen.push(gate.bytes()); await sleep(5); return 1 }, gate)
    expect(await Promise.all([read(), read()])).toEqual([1, 1])
    const one = planCharge([{ key: 'a', rows }])
    // One at a time: the second fits only once the first has given back.
    expect(seen).toEqual([one, one])
    expect(gate.bytes()).toBe(0)
  })

  it('lets a point read past the line, and a held charge\'s reads', async () => {
    const gate = new ReadGate({ ...OPTS, bytes: 10 })
    const full = await gate.admit(10)
    const env = { [REQ]: new Req() }
    expect(await chargedRead(env, [{ key: 'a', rows: READ_SMALL_ROWS }], async () => 'small', gate)).toBe('small')
    env[REQ].reads = new Grant(gate)
    full.release()
    await env[REQ].reads.admit(10)
    expect(await chargedRead(env, [{ key: 'a', rows: READ_SMALL_ROWS + 1 }], async () => 'under the charge', gate)).toBe('under the charge')
    expect(gate.bytes()).toBe(10)
    env[REQ].reads.release()
    await flush()
    expect(gate.bytes()).toBe(0)
  })

  it('counts a group once, however many reads plan it', () => {
    const groups = [{ key: 'bysize|0|1', rows: 1000 }, { key: 'bysize|0|1', rows: 1000 }, { key: 'bysize|1|1', rows: 500 }, { key: 'path|0|1', rows: 250 }]
    expect(planCharge(groups)).toBe(READ_REQUEST_HEAP + 1750 * READ_ROW_HEAP)
  })
})

describe('the read path', () => {
  const held: Ticket[] = []
  const cap0 = { ...READ_CAP }
  const puts: string[] = []

  async function storeEnv(): Promise<Env> {
    const fs = await import(/* @vite-ignore */ 'node:fs' as string) as {
      existsSync(path: string): boolean
      readFileSync(path: string): Uint8Array
    }
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
    return { DB: db, PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g1', INDEX_R2: bucket, STORE_BUCKET: 'b', STORE_ACCESS_KEY_ID: 'k', STORE_SECRET_ACCESS_KEY: 's', [REQ]: new Req() } as unknown as Env
  }

  beforeEach(() => {
    puts.length = 0
    clearIsolate()
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async (k: Request) => { if (k.url.includes('subtree.cache')) puts.push(k.url) } } }
    Object.assign(READ_CAP, { pollMs: 1, waitMs: 2_000 })
  })
  afterEach(() => {
    for (const t of held.splice(0)) t.release()
    Object.assign(READ_CAP, cap0)
  })

  const VIEW = { date: '2026-08-03', path: 'b2/e', w: 8, h: 6, minArea: 12, atten: 2, ivPlanRows: 0 }
  const DIFF = { from: '2026-08-04', to: '2026-08-05', path: 'b2/e', w: 8, h: 6, minArea: 12, atten: 2, top: 100, ivPlanRows: 0 }
  const fresh = (env: Env): Env => ({ ...env, [REQ]: new Req() }) as unknown as Env
  /** The charge a view of `date` asks. */
  const planOf = async (env: Env, date: string): Promise<number> => {
    const e = fresh(env)
    const asked: number[] = []
    const admit = Grant.prototype.admit
    const spy = vi.spyOn(Grant.prototype, 'admit').mockImplementation(function (this: Grant, bytes: number, trace) {
      asked.push(bytes)
      return admit.call(this, bytes, trace)
    })
    try {
      await withReads(e, () => buildView(e, { ...VIEW, path: DIFF.path, date }))
    } finally {
      spy.mockRestore()
    }
    expect(asked.length).toBe(1)
    return asked[0]
  }

  it('answers a view and a diff the same whatever else the isolate holds', async () => {
    const env = await storeEnv()
    const view0 = await withReads(fresh(env), async () => JSON.stringify(await buildView(fresh(env), VIEW)))
    const diff0 = await JSON.stringify(await buildDiff(fresh(env), DIFF))
    clearIsolate()
    // Past the cap: every read below waits for the holder, none is told to read less.
    const big = await readGate.admit(READ_CAP.bytes)
    held.push(big)
    const reads = Array.from({ length: 4 }, (_, i) => {
      const e = fresh(env)
      return withReads(e, async () => JSON.stringify(i % 2 ? await buildDiff(e, DIFF) : await buildView(e, VIEW)))
    })
    await flush()
    expect(readGate.waiting).toBe(4)
    big.release()
    expect(await Promise.all(reads)).toEqual([view0, diff0, view0, diff0])
    await flush()
    expect(readGate.bytes()).toBe(0)
  })

  it('takes a diff\'s whole plan up front, each side\'s groups once, before it reads a group', async () => {
    const env = await storeEnv()
    const traces: Record<string, number> = {}
    const e = fresh(env)
    const asked: number[] = []
    const admit = Grant.prototype.admit
    const spy = vi.spyOn(Grant.prototype, 'admit').mockImplementation(function (this: Grant, bytes: number, trace) {
      asked.push(bytes)
      return admit.call(this, bytes, trace)
    })
    let gets = -1
    try {
      await withReads(e, () => buildDiff(e, { ...DIFF, from: DIFF.to, trace: (k: string, ms: number) => { traces[k] = (traces[k] ?? 0) + ms } }))
      gets = asked.length
    } finally {
      spy.mockRestore()
    }
    // Both sides read the same groups: one ask, one side's charge, not their sum.
    const view = await planOf(env, DIFF.to)
    expect(gets).toBe(1)
    expect(asked[0]).toBe(view)
    await flush()
    expect(readGate.bytes()).toBe(0)
  })

  it('answers 503 on a wait\'s end, retryable and uncached, then the answer once there\'s room', async () => {
    const env = await storeEnv()
    Object.assign(READ_CAP, { waitMs: 20 })
    const big = await readGate.admit(READ_CAP.bytes)
    held.push(big)
    const url = 'https://x/api/subtree?date=2026-08-03&path=b2/e&w=128&h=128'
    const res = await warmSubtree(fresh(env), url)
    expect([res.status, res.headers.get('retry-after'), res.headers.get('cache-control')]).toEqual([503, '3', 'no-store'])
    expect((await res.text()).trim()).toMatch(/^reads busy: no room for \d+ MiB in 20 ms, retry$/)
    expect(puts).toEqual([])
    big.release()
    expect(readGate.bytes()).toBe(0)
    Object.assign(READ_CAP, { waitMs: 2_000 })
    const ok = await warmSubtree(fresh(env), url)
    expect(ok.status).toBe(200)
    expect((await ok.json() as { path: string }).path).toBe('b2/e')
    expect(puts.length).toBe(1)
  })

  it('gives the charge back when the answer is done', async () => {
    const env = await storeEnv()
    const res = await warmSubtree(fresh(env), 'https://x/api/subtree?date=2026-08-03&path=b2/e&w=128&h=128')
    expect(res.status).toBe(200)
    await flush()
    expect(readGate.bytes()).toBe(0)
  })
})

describe('a busy refusal', () => {
  it('is the 503 every read route already answers a broken run with', async () => {
    const res = ivRefusal(new ReadsBusy('3 reads waiting'))!
    expect([res.status, res.headers.get('retry-after'), res.headers.get('cache-control')]).toEqual([503, '3', 'no-store'])
    expect(await res.text()).toBe('reads busy: 3 reads waiting, retry\n')
  })

})
