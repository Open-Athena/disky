/** The Workers digest converge (specs/slack-in-workers.md): a month's thread posted, then converged again as scans land,
 * resumed after a step's budget runs out, and run as a shadow, over an in-memory store and a fake Slack. */
import { describe, expect, it } from 'vitest'
import type { SlackClient } from '@open-athena/thrds'
import { Converger, type ConvergeConfig, type ConvergeDeps, dataBase, leaseKey, relinkText, stateKey, type ThreadState } from './digestConverge'
import type { SnapshotStore } from './digestStore'
import type { QuotaRows, QuotaScan } from '../../src/digest/quota'
import { Budget, OutOfBudget } from './slackJobs'

/** r2's converge config (`dt-cloud digest-config -T quota -C job/digest.yml`), its buckets cut to two. */
const CFG: ConvergeConfig = {
  version: 1, template: 'quota',
  cfg: {
    template: 'quota', title: 'Open Athena R2', site_url: 'https://r2.oa.dev', host: 'r2.oa.dev', unit: { name: 'TB', bytes: 1e12, short: 'T' },
    primary: 'marin-na', buckets: { 'marin-na': { label: 'marin-na', path: null, zone: null, quotas: [] }, 'marin-discord': { label: 'marin-discord', path: null, zone: null, quotas: [] } },
    prices: {}, icons_base: 'https://oa-r2-usage-icons.pages.dev',
  },
  params: '',
  reply: { quota_marks: 'warning', total_headline: false, reply_hour: 0, provisional: false, alert_pct: null, op_sender: { icon_url: 'https://oa-r2-usage-icons.pages.dev/mark.png' }, avatar_rev: 4 },
  root: 'r2://oa-r2-usage-index/snapshots/r2', state: 'digest/r2/{channel}/{variant}', preview: null, variant: 'weekly',
  variants: ['sender', 'body', 'weekly'], edited_variants: ['body', 'weekly'], track_scan: true, pin: true, split_reply: false, discord: false,
  title: 'Open Athena R2', op_sender: { icon_url: 'https://oa-r2-usage-icons.pages.dev/mark.png' },
}

/** A store in memory: versions count writes; a write at a stale version is refused. */
function memStore(): SnapshotStore & { objects: Map<string, { value: unknown; version: string }> } {
  const objects = new Map<string, { value: unknown; version: string }>()
  let n = 0
  return {
    where: 'r2://oa-r2-usage-index', objects,
    list: async prefix => [...objects.keys()].filter(k => k.startsWith(prefix)),
    getText: async key => (objects.has(key) ? JSON.stringify(objects.get(key)!.value) : null),
    getJson: async <T,>(key: string) => (objects.has(key) ? { value: structuredClone(objects.get(key)!.value) as T, version: objects.get(key)!.version } : null),
    putJson: async (key, value, ifVersion) => {
      if ((objects.get(key)?.version ?? null) !== ifVersion) return null
      const version = `v${++n}`
      objects.set(key, { value: structuredClone(value), version })
      return version
    },
  }
}

interface Call { method: string; body: Record<string, unknown> }

/** Slack: posts get sequential ts; every call is recorded; `budget` counts them as a step's client would. */
function fakeSlack(budget?: Budget): SlackClient & { calls: Call[] } {
  let n = 0
  const calls: Call[] = []
  return {
    calls,
    api: async (method: string, body: Record<string, unknown> = {}) => {
      budget?.check()
      calls.push({ method, body })
      if (method === 'chat.postMessage') return { ok: true, ts: `100.${String(++n).padStart(6, '0')}` }
      if (method === 'chat.getPermalink') return { ok: true, permalink: `https://slack.example.org/archives/C1/p${String(body['message_ts']).replace('.', '')}` }
      return { ok: true }
    },
  } as unknown as SlackClient & { calls: Call[] }
}

const scan = (id: string, tb: number): QuotaScan => ({ scan: id, slug: null, tb, extra: { 'marin-discord': 1.1 }, classes: null, objs: 1000 })
/** December's first days, a scan a day at 07Z, after November's last. */
const rows = (days: number): QuotaRows => ({
  template: 'quota', closed: false, pre: [],
  lead: [scan('2026-11-30T0702', 280)],
  rows: Array.from({ length: days }, (_, i) => scan(`2026-12-0${i + 1}T0702`, 280 + 0.4 * (i + 1))),
})

function deps(store: SnapshotStore, slack: SlackClient, over: Partial<ConvergeDeps> = {}): ConvergeDeps {
  return {
    store, slack, channel: 'C1', have: null, settings: null, triggers: {}, memo: {}, log: () => {},
    plot: async () => 'https://r2.oa.dev/a/ab.png', now: () => new Date('2026-12-03T08:00:00Z'), ...over,
  }
}

const KEY = stateKey(CFG, 'C1', 'weekly', '2026-12')
const writes = (calls: Call[]) => calls.filter(c => c.method !== 'chat.getPermalink').map(c => [c.method, c.body['ts'] ?? c.body['thread_ts'] ?? null, c.body['username'] ?? null])

describe('the Workers digest converge', () => {
  it('keys state and the lease as the job does', () => {
    expect([KEY, leaseKey(CFG, 'C1', '2026-12'), stateKey({ ...CFG, root: 'gs://d/snapshots/cw' }, 'C1', 'body', '2026-12'), dataBase('gs://d/x/snapshots/cw')])
      .toEqual(['digest/r2/C1/weekly/2026-12.json', 'digest/r2/C1/2026-12.lease.json', 'digest/cw/C1/body/2026-12.json', 'x'])
  })

  it('posts the OP and a weekly reply, then edits only what a new scan changes', async () => {
    const store = memStore()
    let slack = fakeSlack()
    await new Converger(CFG, deps(store, slack)).converge('2026-12', 'weekly', false, rows(2))
    expect(writes(slack.calls)).toEqual([
      ['chat.postMessage', null, 'Open Athena R2 — December 2026'],
      ['chat.postMessage', '100.000001', 'wk of 11/30'],
    ])
    expect(store.objects.get(KEY)!.value).toEqual({
      variant: 'weekly', op_ts: '100.000001', posted: { 'wk-2026-11-30': { ts: '100.000002', scan: '2026-12-02T0702' } },
    })
    // the same scans again: the OP is edited (its window moves), the reply isn't
    slack = fakeSlack()
    await new Converger(CFG, deps(store, slack)).converge('2026-12', 'weekly', false, rows(2))
    expect(writes(slack.calls)).toEqual([['chat.update', '100.000001', null]])
    // a third day lands: the week's reply is edited to it
    slack = fakeSlack()
    await new Converger(CFG, deps(store, slack)).converge('2026-12', 'weekly', false, rows(3))
    expect(writes(slack.calls)).toEqual([['chat.update', '100.000001', null], ['chat.update', '100.000002', null]])
    expect((store.objects.get(KEY)!.value as ThreadState).posted).toEqual({ 'wk-2026-11-30': { ts: '100.000002', scan: '2026-12-03T0702' } })
  })

  it('a step out of budget resumes where it stopped, posting nothing twice', async () => {
    const store = memStore()
    const memo = {}
    const b1 = new Budget(1, Number.MAX_SAFE_INTEGER)
    const s1 = fakeSlack(b1)
    await expect(new Converger(CFG, deps(store, s1, { memo })).converge('2026-12', 'weekly', false, rows(2))).rejects.toBeInstanceOf(OutOfBudget)
    expect(writes(s1.calls)).toEqual([['chat.postMessage', null, 'Open Athena R2 — December 2026']])
    const s2 = fakeSlack()
    await new Converger(CFG, deps(store, s2, { memo })).converge('2026-12', 'weekly', false, rows(2))
    // the OP isn't edited again within the job (`memo`), the reply is posted once
    expect(writes(s2.calls)).toEqual([['chat.postMessage', '100.000001', 'wk of 11/30']])
  })

  it('pins the current month\'s OP and unpins the last month\'s', async () => {
    const store = memStore()
    await store.putJson(stateKey(CFG, 'C1', 'sender', '2026-11'), { variant: 'sender', op_ts: '90.000001', pinned: '90.000001', prev_url: null, next_url: 'x' }, null)
    const slack = fakeSlack()
    const conv = new Converger(CFG, deps(store, slack))
    conv.linkPrev = async () => {}
    await conv.converge('2026-12', 'weekly', true, rows(1))
    expect(writes(slack.calls).slice(0, 3)).toEqual([
      ['chat.postMessage', null, 'Open Athena R2 — December 2026'],
      ['pins.add', null, null],
      ['pins.remove', null, null],
    ])
    expect(slack.calls.filter(c => c.method.startsWith('pins.')).map(c => c.body['timestamp'])).toEqual(['100.000001', '90.000001'])
    // November's thread (another style) is linked from December's OP
    const op = slack.calls.find(c => c.method === 'chat.postMessage')!.body['text'] as string
    expect(op.split('\n').filter(l => l.includes('November'))).toEqual(['<https://slack.example.org/archives/C1/p90000001|← November>'])
  })

  it('a shadow writes nothing, and records what it would', async () => {
    const store = memStore()
    const slack = fakeSlack()
    const shadow = { writes: [], states: new Map() }
    await new Converger(CFG, deps(store, slack, { shadow })).converge('2026-12', 'weekly', false, rows(2))
    expect([store.objects.size, slack.calls.filter(c => c.method !== 'chat.getPermalink').length]).toEqual([0, 0])
    expect(shadow.writes.map((w: { method: string; body: Record<string, unknown> }) => [w.method, w.body['thread_ts'] ?? null])).toEqual([
      ['chat.postMessage', null], ['chat.postMessage', 'shadow.1'],
    ])
  })
})

describe('relinkText', () => {
  const URL = 'https://slack.example.org/archives/C1/p'
  const line = `<${URL}1|← August> · <${URL}3|October →>`
  it('matches the job\'s', () => {
    expect([
      relinkText(`a\n\n<${URL}1|← August>`, line),
      relinkText(`a\n\n<${URL}1|← August>\n\nb`, line),
      relinkText('a\nb\n', line),
      relinkText(`see <${URL}1|← August> here`, line),
      relinkText(`a\n\n<${URL}1|← August>`, ''),
      relinkText('a', ''),
      relinkText(`a b *Weekly* c <${URL}1|← August>`, line),
      relinkText(`a b <${URL}1|← August> · <${URL}3|October →> `, line),
      relinkText(`a b <${URL}1|← August>`, ''),
    ]).toEqual([
      `a\n\n${line}`, `a\n\n${line}\n\nb`, `a\nb\n\n${line}`, `see <${URL}1|← August> here\n\n${line}`, 'a', 'a',
      `a b *Weekly* c ${line}`, `a b ${line}`, 'a b',
    ])
  })
})
