import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { gateFor } from './auth'
import { sqliteD1 } from './testD1'
import { alertBody, check, counts, decide, MONITOR_END_MS, MONITOR_SCOPE, POLICY, sendAlert, type Counts, type State } from './sessionMonitor'
import { onRequestPost } from '../api/session-monitor'
import { onRequest as sessionLogRoute } from '../api/session-log/[[path]]'
import { onRequestGet as meRoute } from '../api/me'

const NOW = Date.UTC(2026, 9, 13, 22) / 1000
const EMPTY: Counts = { batches: 0, events: 0, exceptions: 0, failures: 0, slow: 0, affected: 0, latest: 0 }
const STATE: State = { version: 0, lease_until: 0, checked_s: 0, signal_s: 0, alert_s: 0, day: '', attempts: 0, thread_ts: null, delivery: 'none' }
const SIGNAL: Counts = { ...EMPTY, batches: 1, events: 3, failures: 3, affected: 1, latest: NOW - 1 }
beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(NOW * 1000) })
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

async function setup() {
  const { db, raw } = await sqliteD1()
  raw.exec('INSERT INTO session_monitor (id) VALUES (1)')  // its one row, as prod has it (seeded when the table was made)
  raw.prepare('INSERT INTO session_log_switch VALUES (1, ?, ?, ?)').run(MONITOR_END_MS, 'test', NOW)
  const env = { DB: db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'gcs', PROD_HOST: 'gcs.oa.dev', SESSION_MONITOR: '1', SLACK_BOT_TOKEN: 'test-token', SLACK_ADMIN_CHANNEL: 'test-channel' }
  const put = (events: Record<string, unknown>[], received = NOW - 1, seq = 0, sid = 'sid-test') =>
    raw.prepare('INSERT INTO session_log_batches VALUES (?, ?, ?, ?, ?, ?, ?, ?)').run(sid, 'pid-test', seq, received, 0, 0, events.length, JSON.stringify(events))
  const state = () => ({ ...raw.prepare('SELECT version, lease_until, checked_s, signal_s, alert_s, day, attempts, thread_ts, delivery FROM session_monitor WHERE id = 1').all()[0] })
  return { db, raw, env, put, state }
}

it('aggregates exactly, ignores aborts/4xx, bounds by received time and never returns private fields', async () => {
  const { db, put } = await setup()
  put([
    { k: 'api', s: 0, err: 'abort' }, { k: 'api', s: 403 }, { k: 'api', s: 404 },
    { k: 'api', s: 0, err: 'private message', u: '/?f=private' },
    { k: 'api', s: 524 }, { k: 'console', m: 'private' },
    { k: 'error', stack: 'private' }, { k: 'reject' }, { k: 'api', s: 200, ms: 10000 },
    { k: 'api', s: 200, ms: 9999 }, { k: 'api', s: 0, ms: 30000, err: 'abort' },
    { k: 'api', s: '524' }, { k: 'api', s: 200, ms: '10000' },
  ])
  put([{ k: 'error' }], NOW - POLICY.window, 1)
  put([{ k: 'error' }], NOW + 1, 2)
  expect(await counts(db, NOW)).toEqual({ batches: 1, events: 13, exceptions: 2, failures: 3, slow: 1, affected: 1, latest: NOW - 1 })
})

it.each([
  [EMPTY, STATE, 'quiet'],
  [{ ...EMPTY, failures: 2, slow: 2 }, STATE, 'quiet'],
  [{ ...EMPTY, exceptions: 1, latest: NOW - 1 }, STATE, 'alert'],
  [{ ...EMPTY, slow: 3, latest: NOW - 1 }, STATE, 'alert'],
  [SIGNAL, { ...STATE, alert_s: NOW - 1 }, 'already-seen'],
  [SIGNAL, { ...STATE, alert_s: NOW - 1800 }, 'cooldown'],
  [SIGNAL, { ...STATE, day: '2026-10-13', attempts: 3 }, 'daily-cap'],
  [SIGNAL, { ...STATE, day: '2026-10-12', attempts: 3 }, 'alert'],
] as const)('decision %j with %j → %s', (c, s, reason) => {
  const result = decide(s, c, NOW)
  expect({ alert: result.alert, reason: result.reason }).toEqual({ alert: reason === 'alert', reason })
})

it('clean and dry checks never send; dry run does not change state', async () => {
  const { db, env, put, state } = await setup()
  const send = vi.fn<typeof sendAlert>()
  put([{ k: 'error' }])
  const prior = state()
  expect(await check(db, env, NOW, true, send)).toEqual({ status: 'dry-run', decision: 'alert', counts: { ...EMPTY, batches: 1, events: 1, exceptions: 1, affected: 1, latest: NOW - 1 } })
  expect(state()).toEqual(prior)
  expect(send.mock.calls).toEqual([])
  expect(await check(db, env, NOW + POLICY.window, false, send)).toEqual({ status: 'quiet', counts: EMPTY })
  expect(send.mock.calls).toEqual([])
})

it('claims the budget before I/O; replay/overlap are quiet; delivery failures consume budget', async () => {
  const { db, env, put, state } = await setup()
  put([{ k: 'api', s: 524 }, { k: 'api', s: 500 }, { k: 'console' }])
  const send = vi.fn<typeof sendAlert>(async () => {
    expect(state()).toEqual({ ...STATE, version: 1, checked_s: NOW, signal_s: NOW - 1, alert_s: NOW, day: '2026-10-13', attempts: 1, lease_until: NOW + POLICY.lease, delivery: 'attempting' })
    expect(await check(db, env, NOW, false, send)).toEqual({ status: 'already-checked' })
    return { ok: false }
  })
  expect(await check(db, env, NOW, false, send)).toEqual({ status: 'delivery-failed-or-unknown', counts: SIGNAL })
  expect(state()).toEqual({ ...STATE, version: 1, checked_s: NOW, signal_s: NOW - 1, alert_s: NOW, day: '2026-10-13', attempts: 1, delivery: 'failed-or-unknown' })
  expect(await check(db, env, NOW + 300, false, send)).toEqual({ status: 'already-seen', counts: SIGNAL })
  expect(send.mock.calls.length).toBe(1)
})

it('ongoing issues follow up only in the original thread and stop at the daily cap', async () => {
  const { db, env, put, state } = await setup()
  const send = vi.fn<typeof sendAlert>(async () => ({ ok: true, ts: 'thread-1' }))
  for (let i = 0; i < 4; i++) {
    const now = NOW + i * POLICY.cooldown
    // Keep the signal continuous between checks, without generating extra alerts.
    if (i) for (let offset = 300; offset < POLICY.cooldown; offset += 300) {
      put([{ k: 'error' }], now - POLICY.cooldown + offset - 1, i * 100 + offset)
      await check(db, env, now - POLICY.cooldown + offset, false, send)
    }
    put([{ k: 'error' }], now - 1, i * 100)
    await check(db, env, now, false, send)
  }
  // Crosses UTC midnight: a new UTC day's budget is deliberately available.
  expect(send.mock.calls.map(args => args[1])).toEqual([
    alertBody({ ...EMPTY, batches: 1, events: 1, exceptions: 1, affected: 1, latest: NOW - 1 }, null, 'test-channel'),
    alertBody({ ...EMPTY, batches: 2, events: 2, exceptions: 2, affected: 1, latest: NOW + 3599 }, 'thread-1', 'test-channel'),
    alertBody({ ...EMPTY, batches: 2, events: 2, exceptions: 2, affected: 1, latest: NOW + 7199 }, 'thread-1', 'test-channel'),
    alertBody({ ...EMPTY, batches: 2, events: 2, exceptions: 2, affected: 1, latest: NOW + 10799 }, 'thread-1', 'test-channel'),
  ])
  expect(state().attempts).toBe(2)
})

it('three attempts exhaust one UTC day; quiet recovery and missing delivery keep safe budgets', () => {
  const prior = { ...STATE, signal_s: NOW - 100, alert_s: NOW - 7200, day: '2026-10-13', attempts: 3, thread_ts: 'thread' }
  expect(decide(prior, SIGNAL, NOW)).toEqual({ alert: false, reason: 'daily-cap', next: { ...prior, version: 1, checked_s: NOW, signal_s: NOW - 1 } })
  expect(decide(prior, EMPTY, NOW + POLICY.quiet).next.thread_ts).toBeNull()
})

it('CAS permits only one concurrently calculated alert', async () => {
  const { db, env, put } = await setup()
  put([{ k: 'error' }])
  const send = vi.fn<typeof sendAlert>(async () => ({ ok: true, ts: 'thread' }))
  const results = await Promise.all([check(db, env, NOW, false, send), check(db, env, NOW, false, send)])
  expect(results.map(x => x.status).sort()).toEqual(['alerted', 'already-checked'])
  expect(send.mock.calls.length).toBe(1)
})

it('off/expired checks do not send or mutate state; excessive logs are refused', async () => {
  const { db, raw, env, put, state } = await setup()
  const send = vi.fn<typeof sendAlert>()
  const prior = state()
  expect(await check(db, env, MONITOR_END_MS / 1000, false, send)).toEqual({ status: 'disabled' })
  raw.exec('UPDATE session_log_switch SET until_ms = NULL')
  expect(await check(db, env, NOW, false, send)).toEqual({ status: 'disabled' })
  expect(state()).toEqual(prior)
  expect(send.mock.calls).toEqual([])
  for (let i = 0; i <= POLICY.batches; i++) put([{ k: 'error' }], NOW - 1, i)
  await expect(counts(db, NOW)).rejects.toThrow('session monitor batch budget exceeded')
})

it.each(['reject', '429', 'bad-json', 'ok-false', 'success'])('Slack %s is a single bounded attempt', async kind => {
  const fake = vi.fn(async () => {
    if (kind === 'reject') throw new Error('unknown delivery')
    return new Response(kind === 'bad-json' ? 'bad' : JSON.stringify(kind === 'success' ? { ok: true, ts: '123.456' } : { ok: false }), { status: kind === '429' ? 429 : 200 })
  })
  vi.stubGlobal('fetch', fake)
  expect(await sendAlert({ SLACK_BOT_TOKEN: 'test' }, {})).toEqual(kind === 'success' ? { ok: true, ts: '123.456' } : { ok: false })
  expect(fake.mock.calls.length).toBe(1)
})

it('credential is monitor-only; viewer/admin cannot trigger it; preview refuses sends, allows safe dry run', async () => {
  const { env } = await setup()
  const gate = gateFor(env)!
  const tokens = await Promise.all([[MONITOR_SCOPE], ['gcs:read'], ['admin']].map(scopes => gate.mint({ name: 'test', createdBy: 'test', scopes, expiresAt: MONITOR_END_MS / 1000, maxRedeems: null })))
  const call = (token: string | undefined, host = 'gcs.oa.dev', path = '/api/session-monitor') => ({ request: new Request(`https://${host}${path}`, { method: 'POST', headers: token ? { authorization: `Bearer ${token}` } : {} }), env })
  const response = async (ctx: ReturnType<typeof call>) => { const r = await onRequestPost(ctx); return [r.status, await r.json()] }
  expect(await response(call(undefined))).toEqual([401, { error: 'unauthenticated' }])
  expect(await response(call(tokens[1].token))).toEqual([403, { error: 'forbidden' }])
  expect(await response(call(tokens[2].token))).toEqual([403, { error: 'forbidden' }])
  expect(await response(call(tokens[0].token, 'dev.gcs.oa.dev'))).toEqual([403, { error: 'monitor notifications disabled on this host' }])
  expect(await response(call(tokens[0].token, 'dev.gcs.oa.dev', '/api/session-monitor?dry=1'))).toEqual([200, { status: 'dry-run', decision: 'quiet', counts: EMPTY }])
  const restricted = call(tokens[0].token)
  expect((await meRoute(restricted)).status).toBe(403)
  expect((await sessionLogRoute({ ...restricted, request: new Request('https://gcs.oa.dev/api/session-log/sessions', { headers: { authorization: `Bearer ${tokens[0].token}` } }), params: { path: ['sessions'] } })).status).toBe(403)
  vi.setSystemTime(MONITOR_END_MS)
  expect(await response(call(undefined))).toEqual([200, { status: 'expired' }])
})
