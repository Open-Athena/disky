import { describe, expect, it, vi } from 'vitest'
import { onRequest } from './[[path]]'
import type { Env } from '../_lib/auth'

const get = vi.hoisted(() => vi.fn(async (key: string) => ({ bytes: new TextEncoder().encode(JSON.stringify({ key })), contentType: 'application/json' })))
vi.mock('../_lib/index.js', () => ({ storeReady: () => true, servedStore: () => ({ get }), pathScans: vi.fn() }))

const env: Env = {
  BASE_SCOPE: 'oa-r2', PUBLIC_READ: '1', SNAPSHOTS_SUBDIR: 'r2',
  STORES_JSON: JSON.stringify({ meta: { scope: 'admin', vars: { SNAPSHOTS_SUBDIR: 'meta' } } }),
}
const request = async (path: string, override: Partial<Env> = {}) => {
  const response = await onRequest({ request: new Request(`https://example.test/data/${path}`), env: { ...env, ...override } })
  return [response.status, await response.text()]
}

describe('snapshot store scope', () => {
  it('serves the primary snapshot and its shared rules, but refuses a named secondary through the primary gate', async () => {
    expect(await request('r2/2026-10-13T1935/age.json')).toEqual([200, '{"key":"snapshots/r2/2026-10-13T1935/age.json"}'])
    expect(await request('rules.json')).toEqual([200, '{"key":"snapshots/rules.json"}'])
    expect(await request('meta/2026-10-13T1935/age.json')).toEqual([404, 'not found'])
    expect(await request('meta/2026-10-13T1935/age.json?store=meta')).toEqual([401, '{"error":"unauthenticated"}\n'])
  })
  it('also refuses named secondary directories when the primary stores snapshots at the bucket root', async () => {
    expect(await request('2026-10-13T1935/age.json', { SNAPSHOTS_SUBDIR: '' })).toEqual([200, '{"key":"snapshots/2026-10-13T1935/age.json"}'])
    expect(await request('meta/2026-10-13T1935/age.json', { SNAPSHOTS_SUBDIR: '' })).toEqual([404, 'not found'])
  })
})
