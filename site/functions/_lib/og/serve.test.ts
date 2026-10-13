import { beforeEach, describe, expect, it, vi } from 'vitest'
import { serveCard, stampPage, type OgEnv } from './serve'

const mocks = vi.hoisted(() => ({
  scan: vi.fn(), card: vi.fn(), stamp: vi.fn(), match: vi.fn(), put: vi.fn(),
}))
vi.mock('./vendor/resvg.wasm', () => ({ default: new Uint8Array() }))
vi.mock('./render', () => ({ ensureWasm: async () => {}, FONT_FILES: [], svgToPng: () => new Uint8Array([1, 2, 3]) }))
vi.mock('./data', () => ({ resolveScan: mocks.scan, mapCard: mocks.card }))
vi.mock('./card', () => ({ cardSvg: () => '<svg/>' }))
vi.mock('../unfurl', () => ({ stampMeta: mocks.stamp }))
vi.mock('./tier', () => ({ pageTier: async () => ({ tier: 'anon', cred: null }), credLive: async () => false }))
vi.mock('../ledger', () => ({ ledgerHead: async () => 0 }))

const env: OgEnv = { OG_CARDS: '1', ROOT_LABEL: 'public R2', STORE_SCHEME: 'r2://', ASSETS: { fetch: async () => new Response('') } }

beforeEach(() => {
  vi.clearAllMocks()
  mocks.scan.mockResolvedValue('2026-10-13')
  mocks.card.mockResolvedValue({ tier: 'anon', site: 'public R2', title: 'public R2', tiles: [], subtitle: '', total: '' })
  mocks.stamp.mockImplementation((response: Response) => response)
  mocks.match.mockResolvedValue(undefined)
  mocks.put.mockResolvedValue(undefined)
  vi.stubGlobal('caches', { default: { match: mocks.match, put: mocks.put } })
})

describe('public R2 dynamic metadata', () => {
  it('stamps an anonymous scan-versioned card without a signing secret', async () => {
    const response = await stampPage(env, new URL('https://r2.example/?c=t'), new Response('shell'))
    const meta = mocks.stamp.mock.calls[0][1]
    const image = new URL(meta.image)
    expect({ title: meta.title, image: { origin: image.origin, path: image.pathname, params: Object.fromEntries(image.searchParams) }, cache: response.headers.get('cache-control') }).toEqual({
      title: 'public R2', image: { origin: 'https://r2.example', path: '/og/map.png', params: { c: 't', s: '2026-10-13', r: '5' } }, cache: 'private, no-store',
    })
  })

  it('leaves static metadata untouched when the feature is disabled', async () => {
    const shell = new Response('shell')
    expect(await stampPage({ ...env, OG_CARDS: undefined }, new URL('https://r2.example/'), shell)).toBe(shell)
    expect(mocks.stamp.mock.calls).toEqual([])
  })

  it.each([
    ['?c=t&s=2026-10-12&r=5', '2026-10-12', 'public, max-age=86400'],
    ['?c=w', '2026-10-13', 'public, max-age=0, must-revalidate'],
  ])('renders %s using its scan and color and safe freshness policy', async (query, date, cache) => {
    const response = await serveCard({ request: new Request(`https://r2.example/og/map.png${query}`), env })
    expect(mocks.card.mock.calls.map(call => ({ params: call[3], tier: call[4] }))).toEqual([{ params: { c: new URLSearchParams(query).get('c'), d: date }, tier: 'anon' }])
    expect([response.status, response.headers.get('content-type'), response.headers.get('x-og-tier'), response.headers.get('cache-control')]).toEqual([200, 'image/png', 'anon', cache])
    const key = new URL(mocks.match.mock.calls[0][0].url)
    expect(Object.fromEntries(key.searchParams)).toEqual({ c: new URLSearchParams(query).get('c'), s: date, r: '5', tier: 'anon', h: '0' })
  })
})
