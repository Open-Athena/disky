import { beforeEach, describe, expect, it } from 'vitest'
import { CACHE_V, cacheEnvTag, cacheKeyFor, cacheMatch, cacheStore, keepFor, LEASE_TTL, PARTIAL_TTL, resetUpgrades, unionRetryAfter, UPGRADE_EXPECT_MS, UPGRADE_LATE_MS, upgradeLease, upgradePartial, type UpgradeState, withUpgrade } from './edgeCache'

// `cacheStore` keeps an answer in the colo cache and KV; a budget-cut answer (a filter view whose phase 2 its
// time budget cut short) in the colo cache only, for `PARTIAL_TTL` seconds; `false`: served, not kept.
describe('cacheStore', () => {
  const setup = () => {
    const colo: string[] = []
    const kv: string[] = []
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async (k: Request) => { colo.push(k.url) } } }
    const env = { CACHE_KV: { put: async (k: string) => { kv.push(k) } } as unknown as KVNamespace }
    return { colo, kv, env }
  }
  const key = cacheKeyFor('subtree', '', 'd/p?q=x')
  // The awaited put's duration varies: `awaited;dur=<ms>`.
  const seen = async (r: Response) => [r.headers.get('x-cache'), r.headers.get('x-cache-store')?.replace(/dur=\d+/, 'dur=<ms>'), r.headers.get('server-timing'), await r.text()]

  it('stores in both tiers by default', async () => {
    const { colo, kv, env } = setup()
    const r = await cacheStore(env, key, '{"a":1}', { 'server-timing': 'total;dur=5' })
    expect([await seen(r), colo, kv.length]).toEqual([['miss', 'awaited;dur=<ms>', 'total;dur=5', '{"a":1}'], [key.url], 1])
  })

  it('`store` false: served, in neither tier', async () => {
    const { colo, kv, env } = setup()
    const r = await cacheStore(env, key, '{"a":1}', { 'server-timing': 'total;dur=5' }, undefined, false)
    expect([await seen(r), colo, kv]).toEqual([['miss', 'skipped', 'total;dur=5', '{"a":1}'], [], []])
  })

  it('a budget-cut answer (`keepFor(interiors.late)`): colo only, `PARTIAL_TTL` s, flagged on the miss and on the hit', async () => {
    const stored: [string, string | null, string | null, string][] = []
    const held = new Map<string, Response>()
    ;(globalThis as unknown as { caches: unknown }).caches = { default: {
      match: async (k: Request) => held.get(k.url)?.clone(),
      put: async (k: Request, r: Response) => { stored.push([k.url, r.headers.get('cache-control'), r.headers.get('x-cache-partial'), await r.clone().text()]); held.set(k.url, r) },
    } }
    const kv: string[] = []
    const env = { CACHE_KV: { put: async (k: string) => { kv.push(k) }, get: async () => null } as unknown as KVNamespace }
    const keep = keepFor({ interiors: { late: 3 } })
    const miss = await cacheStore(env, key, '{"budgetCut":true}', {}, undefined, keep)
    const hit = (await cacheMatch(env, key))!
    const flags = async (r: Response) => [r.headers.get('x-cache'), r.headers.get('x-cache-store')?.replace(/dur=\d+/, 'dur=<ms>') ?? null, r.headers.get('x-cache-partial'), r.headers.get('cache-control'), await r.text()]
    expect([keep, PARTIAL_TTL, await flags(miss), await flags(hit), stored, kv]).toEqual([
      { partialTtl: 120 }, 120,
      ['miss', 'partial;ttl=120;awaited;dur=<ms>', 'ttl=120', 'no-store', '{"budgetCut":true}'],
      ['hit', null, 'ttl=120', 'no-store', '{"budgetCut":true}'],
      [[key.url, 'public, s-maxage=120', 'ttl=120', '{"budgetCut":true}']],
      [],
    ])
  })

  it('`keepFor`: whole answers (no interiors, or only read-budget skips) are kept in both tiers; a time cut briefly', () => {
    expect([keepFor({}), keepFor({ interiors: {} }), keepFor({ interiors: { late: 0 } }), keepFor({ interiors: { late: 1 } }), keepFor({ searchLate: true }), keepFor({ detailsLate: true }), keepFor({ lookups_late: true })]).toEqual([true, true, true, { partialTtl: PARTIAL_TTL }, { partialTtl: PARTIAL_TTL }, { partialTtl: PARTIAL_TTL }, { partialTtl: PARTIAL_TTL }])
  })
})

// `upgradePartial`: after a partial answer is served, the full one computed in the background replaces it; one run per
// key across the colo's isolates (`upgradeLease`), and `Retry-After` from what is running.
describe('upgradePartial', () => {
  const key = cacheKeyFor('subtree', '', 'd/p?q=heavy')
  const setup = () => {
    const held = new Map<string, Response>()
    ;(globalThis as unknown as { caches: unknown }).caches = { default: {
      match: async (k: Request) => held.get(k.url)?.clone(),
      put: async (k: Request, r: Response) => { held.set(k.url, r) },
      delete: async (k: Request) => held.delete(k.url),
    } }
    const kv: string[] = []
    const env = { CACHE_KV: { put: async (_k: string, v: string) => { kv.push(v) }, get: async () => null } as unknown as KVNamespace }
    const bg: Promise<unknown>[] = []
    const waitUntil = (p: Promise<unknown>) => { bg.push(p) }
    /** The colo entry: its cache-control, partial flag and body. */
    const entry = async () => {
      const r = held.get(key.url)
      return r ? [r.headers.get('cache-control'), r.headers.get('x-cache-partial'), await r.clone().text()] : null
    }
    /** The lease's colo entry: its cache-control and body (the run's start). */
    const lease = async () => {
      const r = held.get(upgradeLease(key).url)
      return r ? [r.headers.get('cache-control'), await r.clone().text()] : null
    }
    return { env, kv, bg, waitUntil, entry, lease }
  }
  const PARTIAL = '{"budgetCut":true}', FULL = '{"tree":"all"}'
  /** Another isolate: its own (empty) in-flight and gave-up state; the colo cache is the same. */
  const otherIsolate = () => resetUpgrades()
  beforeEach(() => resetUpgrades())

  it('the full answer replaces the partial in the colo cache at the normal TTL (and goes to KV)', async () => {
    const { env, kv, bg, waitUntil, entry } = setup()
    await cacheStore(env, key, PARTIAL, {}, undefined, keepFor({ interiors: { late: 2 } }))
    const before = await entry()
    const runs: number[] = []
    const got = await upgradePartial(env, key, async () => { runs.push(1); return { body: FULL, keep: true } }, waitUntil)
    await Promise.all(bg)
    const hit = (await cacheMatch(env, key))!
    expect([before, got, runs.length, await entry(), kv, hit.headers.get('x-cache-partial'), await hit.text()]).toEqual([
      ['public, s-maxage=120', 'ttl=120', PARTIAL],
      { upgrade: 'scheduled', retryAfterMs: UPGRADE_EXPECT_MS }, 1,
      ['public, s-maxage=86400', null, FULL],
      [FULL],
      null, FULL,
    ])
  })

  it('dedupe: concurrent partial hits while a run is in flight schedule nothing more, and say when it should be done; after it, a new one may', async () => {
    const { env, bg, waitUntil } = setup()
    let release!: () => void
    const gate = new Promise<void>(r => { release = r })
    let runs = 0
    const compute = async () => { runs++; await gate; return { body: FULL, keep: true as const } }
    const t0 = 1_000_000
    const first = [await upgradePartial(env, key, compute, waitUntil, t0), await upgradePartial(env, key, compute, waitUntil, t0 + 2000), await upgradePartial(env, key, compute, waitUntil, t0 + 6000)]
    const other = await upgradePartial(env, cacheKeyFor('subtree', '', 'd/p?q=other'), async () => ({ body: FULL, keep: true }), waitUntil, t0)
    release()
    await Promise.all(bg)
    const after = await upgradePartial(env, key, compute, waitUntil, t0 + 20_000)
    await Promise.all(bg)
    expect([first, other, after, runs]).toEqual([
      [{ upgrade: 'scheduled', retryAfterMs: UPGRADE_EXPECT_MS }, { upgrade: 'in-flight', retryAfterMs: UPGRADE_EXPECT_MS - 2000 }, { upgrade: 'in-flight', retryAfterMs: UPGRADE_LATE_MS }],
      { upgrade: 'scheduled', retryAfterMs: UPGRADE_EXPECT_MS },
      { upgrade: 'scheduled', retryAfterMs: UPGRADE_EXPECT_MS },
      2,
    ])
  })

  it('the colo lease: another isolate that finds a run on starts none and says when it should be done; once it ends, or past `LEASE_TTL`, one may', async () => {
    const { env, bg, waitUntil, lease } = setup()
    let release!: () => void
    const gate = new Promise<void>(r => { release = r })
    let runs = 0
    const compute = async () => { runs++; await gate; return { body: FULL, keep: true as const } }
    const t0 = 1_000_000
    const first = await upgradePartial(env, key, compute, waitUntil, t0)
    const during = await lease()
    otherIsolate()
    const followers = [await upgradePartial(env, key, compute, waitUntil, t0 + 1000), await upgradePartial(env, key, compute, waitUntil, t0 + 9000)]
    const runsDuring = runs
    release()
    await Promise.all(bg)
    const ended = await lease()
    otherIsolate()
    const next = await upgradePartial(env, key, async () => { runs++; return { body: FULL, keep: true } }, waitUntil, t0 + 20_000)
    await Promise.all(bg)
    // A lease its run never cleared (the isolate died with it) holds the others off only `LEASE_TTL`.
    const t1 = 2_000_000
    otherIsolate()
    await upgradePartial(env, key, () => new Promise(() => {}), waitUntil, t1)
    otherIsolate()
    const stale = [await upgradePartial(env, key, compute, waitUntil, t1 + LEASE_TTL * 1000 - 1), await upgradePartial(env, key, async () => ({ body: FULL, keep: true }), waitUntil, t1 + LEASE_TTL * 1000)]
    expect([first, during, followers, runsDuring, ended, next, runs, stale]).toEqual([
      { upgrade: 'scheduled', retryAfterMs: UPGRADE_EXPECT_MS },
      [`public, s-maxage=${LEASE_TTL}`, String(t0)],
      [{ upgrade: 'leased', retryAfterMs: UPGRADE_EXPECT_MS - 1000 }, { upgrade: 'leased', retryAfterMs: UPGRADE_LATE_MS }],
      1,
      null,
      { upgrade: 'scheduled', retryAfterMs: UPGRADE_EXPECT_MS },
      2,
      [{ upgrade: 'leased', retryAfterMs: UPGRADE_LATE_MS }, { upgrade: 'scheduled', retryAfterMs: UPGRADE_EXPECT_MS }],
    ])
  })

  it('a background run cut short too: the partial stays, no rerun until it would have expired, and no `Retry-After` meanwhile', async () => {
    const { env, bg, waitUntil, entry, lease } = setup()
    await cacheStore(env, key, PARTIAL, {}, undefined, keepFor({ interiors: { late: 2 } }))
    let runs = 0
    const cut = async () => { runs++; return { body: '{"budgetCut":true,"more":1}', keep: keepFor({ interiors: { late: 1 } }) } }
    const t0 = 1_000_000
    const got = await upgradePartial(env, key, cut, waitUntil, t0)
    await Promise.all(bg)
    const again = [await upgradePartial(env, key, cut, waitUntil, t0 + 1000), await upgradePartial(env, key, cut, waitUntil, t0 + PARTIAL_TTL * 1000 - 1)]
    const expired = await upgradePartial(env, key, cut, waitUntil, t0 + PARTIAL_TTL * 1000)
    await Promise.all(bg)
    expect([got, again, expired, runs, await entry(), await lease()]).toEqual([
      { upgrade: 'scheduled', retryAfterMs: UPGRADE_EXPECT_MS },
      [{ upgrade: 'gave-up' }, { upgrade: 'gave-up' }],
      { upgrade: 'scheduled', retryAfterMs: UPGRADE_EXPECT_MS },
      2,
      ['public, s-maxage=120', 'ttl=120', PARTIAL],
      null,
    ])
  })

  it('a background run that throws: the partial stays, no unhandled rejection, treated as cut short', async () => {
    const { env, bg, waitUntil, entry } = setup()
    await cacheStore(env, key, PARTIAL, {}, undefined, keepFor({ interiors: { late: 2 } }))
    const got = await upgradePartial(env, key, async () => { throw new Error('D1_ERROR') }, waitUntil, 0)
    await Promise.all(bg)
    expect([got, await upgradePartial(env, key, async () => ({ body: FULL, keep: true }), waitUntil, 1), await entry()]).toEqual([
      { upgrade: 'scheduled', retryAfterMs: UPGRADE_EXPECT_MS }, { upgrade: 'gave-up' }, ['public, s-maxage=120', 'ttl=120', PARTIAL],
    ])
  })

  it('without `waitUntil` nothing is scheduled', async () => {
    const { env } = setup()
    let runs = 0
    expect([await upgradePartial(env, key, async () => { runs++; return { body: FULL, keep: true } }, undefined), runs]).toEqual([{ upgrade: 'no-waituntil' }, 0])
  })

  it('`withUpgrade`: `x-cache-upgrade`, and `Retry-After` in whole seconds (up) only while a run is on', () => {
    const h = (u: UpgradeState) => { const r = withUpgrade(new Response('{}'), u); return [r.headers.get('x-cache-upgrade'), r.headers.get('retry-after')] }
    expect([h({ upgrade: 'scheduled', retryAfterMs: 7000 }), h({ upgrade: 'leased', retryAfterMs: 4001 }), h({ upgrade: 'gave-up' }), h({ upgrade: 'no-waituntil' })]).toEqual([
      ['scheduled', '7'], ['leased', '5'], ['gave-up', null], ['no-waituntil', null],
    ])
  })

  it('`unionRetryAfter`: the members\' latest; none when none says one', () => {
    const r = (s?: string) => new Response('{}', { headers: s ? { 'retry-after': s } : {} })
    expect([unionRetryAfter([r('3'), r(), r('7')]), unionRetryAfter([r(), r()])]).toEqual([{ 'retry-after': '7' }, {}])
  })
})

// A Pages preview (the dev stack) binds its prod's `CACHE_KV` and shares its zone's colo cache: its keys carry
// a `~<env>/` tag, prod's are exactly what they were (no flush).
describe('the deployment-environment tag', () => {
  const PROD = { PROD_HOST: 'gcs.oa.dev' }
  const req = (host: string) => new Request(`https://${host}/api/subtree?date=d&path=p`)
  const sha = async (s: string) => [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s)))].map(b => b.toString(16).padStart(2, '0')).join('')

  it('`cacheEnvTag`: prod\'s host is `\'\'`; any other host (the dev host, a `*.pages.dev` alias, a preview env without `PROD_HOST`) is itself; `CACHE_NS` wins', () => {
    expect([
      cacheEnvTag(PROD, req('gcs.oa.dev')),
      cacheEnvTag(PROD, req('dev.gcs.oa.dev')),
      cacheEnvTag(PROD, req('oa-gcs-usage.pages.dev')),
      cacheEnvTag({}, req('dev.gcs.oa.dev')),
      cacheEnvTag({}, req('gcs.oa.dev')),
      cacheEnvTag({ CACHE_NS: '' }, req('r2.rbw.sh')),
      cacheEnvTag({ ...PROD, CACHE_NS: 'staging' }, req('gcs.oa.dev')),
    ]).toEqual(['', 'dev.gcs.oa.dev', 'oa-gcs-usage.pages.dev', 'dev.gcs.oa.dev', 'gcs.oa.dev', '', 'staging'])
  })

  it('prod\'s keys (colo URL and KV hash) are unchanged; a preview\'s get a `~<tag>/` segment, before a store\'s', () => {
    const prod = cacheKeyFor('subtree', cacheEnvTag(PROD, req('gcs.oa.dev')), 'd/p?w=1')
    const dev = cacheKeyFor('subtree', cacheEnvTag({}, req('dev.gcs.oa.dev')), 'd/p?w=1')
    expect([
      prod.url,
      dev.url,
      cacheKeyFor('subtree', 'dev.gcs.oa.dev', 'd/p?w=1', 'meta').url,
      cacheKeyFor('subtree', 'a/b', 'd/p?w=1').url,
    ]).toEqual([
      `https://subtree.cache/v${CACHE_V}/d/p?w=1`,
      `https://subtree.cache/v${CACHE_V}/~dev.gcs.oa.dev/d/p?w=1`,
      `https://subtree.cache/v${CACHE_V}/~dev.gcs.oa.dev/@meta/d/p?w=1`,
      `https://subtree.cache/v${CACHE_V}/~a%2Fb/d/p?w=1`,
    ])
  })

  it('an answer stored on the dev host is a miss on prod (colo and KV), and vice versa', async () => {
    const colo = new Map<string, Response>()
    const kv = new Map<string, string>()
    ;(globalThis as unknown as { caches: unknown }).caches = { default: {
      match: async (k: Request) => colo.get(k.url)?.clone(),
      put: async (k: Request, r: Response) => { colo.set(k.url, r) },
    } }
    const env = { ...PROD, CACHE_KV: { get: async (k: string) => kv.get(k) ?? null, put: async (k: string, v: string) => { kv.set(k, v) } } as unknown as KVNamespace }
    const key = (host: string) => cacheKeyFor('diff', cacheEnvTag(env, req(host)), 'a/b/p?w=1')
    await cacheStore(env, key('dev.gcs.oa.dev'), '{"dev":1}')
    const prodMiss = await cacheMatch(env, key('gcs.oa.dev'))
    colo.clear()
    const prodKvMiss = await cacheMatch(env, key('gcs.oa.dev'))
    await cacheStore(env, key('gcs.oa.dev'), '{"prod":1}')
    colo.clear()
    const devHit = await cacheMatch(env, key('dev.gcs.oa.dev'))
    const prodHit = await cacheMatch(env, key('gcs.oa.dev'))
    expect([prodMiss, prodKvMiss, await devHit!.text(), await prodHit!.text(), [...kv.keys()].sort()]).toEqual([
      null, null, '{"dev":1}', '{"prod":1}',
      [await sha(`https://diff.cache/v${CACHE_V}/~dev.gcs.oa.dev/a/b/p?w=1`), await sha(`https://diff.cache/v${CACHE_V}/a/b/p?w=1`)].sort(),
    ])
  })
})
