/** Exercise the installed shared client through its public boot API, including its server field whitelist. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { validateBatch, type SlogBatch } from '@open-athena/auth/session-log'

const NOW = Date.UTC(2026, 10, 9, 23, 4)
const UNTIL = NOW + 86_400_000
let stop = () => {}
beforeEach(() => { vi.resetModules(); vi.useFakeTimers(); vi.setSystemTime(NOW) })
afterEach(() => { stop(); vi.unstubAllGlobals(); vi.useRealTimers() })

async function rig() {
  const batches: SlogBatch[] = []
  const nav = { onLine: true }
  const doc = Object.assign(new EventTarget(), { visibilityState: 'visible', referrer: '', wasDiscarded: true })
  let answer: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response> = async () => new Response('{}')
  const fetch0 = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/session-log') {
      if (init?.method === 'POST') { batches.push(JSON.parse(String(init.body))); return new Response(null, { status: 204 }) }
      return new Response(JSON.stringify({ enabled: true, until: UNTIL, appEvents: {} }))
    }
    return answer(input, init)
  })
  const win = Object.assign(new EventTarget(), {
    location: { origin: 'https://site.test', href: 'https://site.test/marin-na' },
    history: { pushState() {}, replaceState() {} },
    innerWidth: 1200, innerHeight: 800, devicePixelRatio: 1,
    navigator: nav, fetch: fetch0 as typeof fetch,
  })
  let perf = 0
  for (const [key, value] of Object.entries({ window: win, document: doc, navigator: nav, fetch: fetch0,
    addEventListener: win.addEventListener.bind(win), removeEventListener: win.removeEventListener.bind(win),
    performance: { now: () => perf },
  })) vi.stubGlobal(key, value)
  const client = await import('@open-athena/auth/session-log/client')
  stop = () => { vi.setSystemTime(UNTIL); client.slog('pick', { n: 'stop', v: 'stop' }) }
  expect(await client.bootSessionLog({ build: 'diagnostics' })).toBe(true)
  const flush = async () => { win.dispatchEvent(new Event('pagehide')); await Promise.resolve() }
  return { win, doc, nav, batches, flush,
    answer: (fn: typeof answer) => { answer = fn },
    perf: (ms: number) => { perf = ms },
    events: () => batches.flatMap(batch => batch.events),
  }
}

it('captures connectivity changes, lifecycle gaps and exact start/end request context', async () => {
  const r = await rig()
  let reject!: (error: Error) => void
  r.answer(() => new Promise((_, no) => { reject = no }))
  const pending = r.win.fetch('/api/series?path=marin-na').catch(() => {})
  r.nav.onLine = false
  r.win.dispatchEvent(new Event('offline'))
  r.doc.visibilityState = 'hidden'
  r.doc.dispatchEvent(new Event('visibilitychange'))
  r.doc.dispatchEvent(new Event('freeze'))
  vi.setSystemTime(NOW + 2400); r.perf(2400)
  reject(new TypeError('Failed to fetch'))
  await pending
  await vi.advanceTimersByTimeAsync(5001)
  vi.setSystemTime(NOW + 48 * 60_000)
  r.nav.onLine = true
  r.win.dispatchEvent(new Event('online'))
  r.doc.visibilityState = 'visible'
  r.doc.dispatchEvent(new Event('resume'))
  r.answer(async () => new Response('{}', { headers: { 'cf-ray': '1234567890abcdef-EWR' } }))
  await r.win.fetch('/api/series?path=marin-na')
  await r.flush()
  expect(r.events().filter(e => e.k === 'api')).toEqual([
    { t: NOW, k: 'api', id: 1, on0: true, vis0: 'visible', visMs: 0, u: '/api/series?path=marin-na', end: NOW + 2400, wall: 2400, on: false, vis: 'hidden', s: 0, ms: 2400, err: 'TypeError: Failed to fetch' },
    { t: NOW + 48 * 60_000, k: 'api', id: 2, on0: true, vis0: 'visible', visMs: 0, gapMs: 48 * 60_000, gapAgo: 0, u: '/api/series?path=marin-na', end: NOW + 48 * 60_000, wall: 0, on: true, vis: 'visible', s: 200, ms: 0, rt: 'default', ray: '1234567890abcdef-EWR' },
  ])
  expect(r.events().filter(e => e.k === 'life').map(({ s, on, vis, ms, discarded }) => ({ s, on, vis, ms, discarded }))).toEqual([
    { s: 'init', on: true, vis: 'visible', ms: undefined, discarded: true },
    { s: 'offline', on: false, vis: 'visible', ms: undefined, discarded: undefined },
    { s: 'freeze', on: false, vis: 'hidden', ms: undefined, discarded: undefined },
    { s: 'gap', on: true, vis: 'hidden', ms: 48 * 60_000, discarded: undefined },
    { s: 'online', on: true, vis: 'hidden', ms: undefined, discarded: undefined },
    { s: 'resume', on: true, vis: 'visible', ms: undefined, discarded: undefined },
    { s: 'pagehide', on: true, vis: 'visible', ms: undefined, discarded: undefined },
  ])
  expect(r.batches.map(batch => validateBatch(batch))).toEqual(r.batches.map(batch => ({ batch })))
})

it('records an aborted signal without copying its arbitrary reason or request credentials', async () => {
  const r = await rig()
  const controller = new AbortController()
  controller.abort(new Error('PRIVATE REASON'))
  r.answer(async () => { throw new TypeError('Failed to fetch') })
  await r.win.fetch('/api/read?key=PRIVATE', { signal: controller.signal, headers: { authorization: 'Bearer PRIVATE' } }).catch(() => {})
  await r.flush()
  expect(r.events().filter(e => e.k === 'api')).toEqual([
    { t: NOW, k: 'api', id: 1, on0: true, vis0: 'visible', visMs: 0, u: '/api/read?key=…', end: NOW, wall: 0, on: true, vis: 'visible', s: 0, ms: 0, err: 'abort', cancel: 'signal' },
  ])
})

it('keeps only a valid Cloudflare Ray header, not arbitrary response headers', async () => {
  const r = await rig()
  r.answer(async () => new Response('{}', { headers: { 'cf-ray': 'PRIVATE', 'set-cookie': 'PRIVATE' } }))
  await r.win.fetch('/api/read')
  await r.flush()
  expect(r.events().filter(e => e.k === 'api')).toEqual([
    { t: NOW, k: 'api', id: 1, on0: true, vis0: 'visible', visMs: 0, u: '/api/read', end: NOW, wall: 0, on: true, vis: 'visible', s: 200, ms: 0, rt: 'default' },
  ])
})

it('observes a visibility transition before query-library refetches run ahead of its own listener', async () => {
  const r = await rig()
  r.doc.visibilityState = 'hidden'
  r.doc.dispatchEvent(new Event('visibilitychange'))
  vi.setSystemTime(NOW + 1000)
  r.doc.visibilityState = 'visible'
  await r.win.fetch('/api/read')
  await r.flush()
  expect(r.events().filter(e => e.k === 'api').map(({ vis0, visMs }) => ({ vis0, visMs }))).toEqual([{ vis0: 'visible', visMs: 0 }])
})
