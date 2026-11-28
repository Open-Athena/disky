/**
 * Slack jobs (specs/slack-in-workers.md): Slack work the site does in steps, each a request, over a D1 row
 * (`slack_jobs`, recipe `slack-jobs`). A job is a converge: its progress is what it has written (the digest's thread
 * state), so a step converges again from there, and the row holds only what a step needs to go on: a lease (one step
 * at a time), what the job mustn't repeat (`memo`), its Slack call count, when Slack said to come back (`retry_at`),
 * and a shadow run's report. Whoever started a job drives it, one `step` per request, until it's done: the scan job's
 * `dt-cloud digest-cfn`, or `/admin/slack`.
 *
 * A step stops at `SLACK_STEP_CALLS` Slack calls (default 40) or `STEP_WALL_MS` of wall time, whichever first, and
 * never sleeps out a `Retry-After` past `MAX_WAIT_MS`: it records `retry_at` and returns, and its driver comes back.
 */
import type { D1Database } from '@cloudflare/workers-types'
import { SlackApiError, SlackClient } from '@open-athena/thrds'
import { fallback, workspaceCustom } from '@disk-tree/pages-kit/emoji'
import {
  Converger, convergeConfig, type ConvergeConfig, desiredVariant, type JobMemo, leaseKey, liveOf, priorMonth,
  type ShadowWrite, stateKey, threadStates, type ThreadState, unsupported,
} from './digestConverge.js'
import { type SnapshotStore, storeFor, type StoreEnv } from './digestStore.js'
import { postPayload } from './digestWire.js'
import { scanTriggers } from '../../src/digest/load.js'

export const TABLE = 'slack_jobs'
export const DEFAULT_STEP_CALLS = 40
export const STEP_WALL_MS = 20_000
export const MAX_WAIT_MS = 10_000
/** A step's lease: longer than a step can run, so a crashed step's job is taken again soon after. */
export const LEASE_MS = 60_000

export type JobState = 'pending' | 'running' | 'done' | 'failed'
export type JobKind = 'digest-converge'

export interface SlackJob {
  id: string; kind: JobKind; key: string; args: string; state: JobState; shadow: number; memo: string; report: string | null
  lease_until: number; retry_at: number | null; calls: number; steps: number; error: string | null
  created_by: string; created_at: number; updated_at: number
}

export interface DigestArgs { month: string; current: boolean }

/** The job's id: its kind, its thread and when it was made. */
const newId = (kind: string, now: number): string => `${kind}-${new Date(now).toISOString().replace(/[-:.]/g, '').toLowerCase()}-${crypto.randomUUID().slice(0, 8)}`

/** The live job for ``key``, else a new one; a race for one key ends with both callers on the same row. */
export async function createOrJoin(db: D1Database, j: { kind: JobKind; key: string; args: unknown; shadow: boolean; by: string }, now = Date.now()): Promise<SlackJob> {
  const live = () => db.prepare(`SELECT * FROM ${TABLE} WHERE key = ? AND state IN ('pending', 'running')`).bind(j.key).first<SlackJob>()
  const have = await live()
  if (have) return have
  const id = newId(j.kind, now)
  try {
    await db.prepare(`INSERT INTO ${TABLE} (id, kind, key, args, state, shadow, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)`)
      .bind(id, j.kind, j.key, JSON.stringify(j.args), j.shadow ? 1 : 0, j.by, now, now).run()
  } catch (e) {
    if (!/UNIQUE/i.test(String((e as Error)?.message))) throw e
    const raced = await live()
    if (raced) return raced
    throw e
  }
  return (await db.prepare(`SELECT * FROM ${TABLE} WHERE id = ?`).bind(id).first<SlackJob>())!
}

export const getJob = (db: D1Database, id: string): Promise<SlackJob | null> => db.prepare(`SELECT * FROM ${TABLE} WHERE id = ?`).bind(id).first<SlackJob>()

export async function recentJobs(db: D1Database, limit = 50): Promise<SlackJob[]> {
  return (await db.prepare(`SELECT * FROM ${TABLE} ORDER BY updated_at DESC LIMIT ?`).bind(limit).all<SlackJob>()).results
}

/** The job, leased for one step; null when it's done, failed, leased by another step, or asked to wait. */
async function lease(db: D1Database, id: string, now: number): Promise<SlackJob | null> {
  return db.prepare(`UPDATE ${TABLE} SET state = 'running', lease_until = ?, steps = steps + 1, updated_at = ? WHERE id = ? AND state IN ('pending', 'running') AND lease_until < ? AND (retry_at IS NULL OR retry_at <= ?) RETURNING *`)
    .bind(now + LEASE_MS, now, id, now, now).first<SlackJob>()
}

interface StepEnd { state: JobState; memo: JobMemo; report?: unknown; calls: number; retryAt?: number | null; error?: string | null }

async function release(db: D1Database, id: string, e: StepEnd, now: number): Promise<SlackJob> {
  return (await db.prepare(`UPDATE ${TABLE} SET state = ?, memo = ?, report = COALESCE(?, report), calls = calls + ?, retry_at = ?, error = ?, lease_until = 0, updated_at = ? WHERE id = ? RETURNING *`)
    .bind(e.state, JSON.stringify(e.memo), e.report === undefined ? null : JSON.stringify(e.report), e.calls, e.retryAt ?? null, e.error ?? null, now, id).first<SlackJob>())!
}

/** A step's Slack budget: `beforeCall` counts each call and refuses the one past it (`OutOfBudget`), or past the wall. */
export class OutOfBudget extends Error {}
export class Budget {
  calls = 0
  constructor(readonly max: number, readonly until: number, readonly now: () => number = Date.now) {}
  check = (): void => {
    if (this.calls >= this.max) throw new OutOfBudget(`step budget: ${this.max} Slack calls`)
    if (this.now() >= this.until) throw new OutOfBudget(`step budget: ${STEP_WALL_MS / 1000} s`)
    this.calls += 1
  }
}

export interface DigestJobEnv extends StoreEnv {
  DB?: D1Database
  SLACK_BOT_TOKEN?: string
  SLACK_DIGEST_CHANNEL?: string
  DIGEST_CONFIG?: string
  /** `"1"`: converges write; `"shadow"`: a converge records the writes it would make, and audits the thread against
   *  what it would render (the dev stack's mode: it never writes the prod thread). Unset: no Worker converge. */
  DIGEST_CFN?: string
  SLACK_STEP_CALLS?: string
}

export const cfnMode = (env: DigestJobEnv): 'live' | 'shadow' | null =>
  env.DIGEST_CFN === '1' ? 'live' : env.DIGEST_CFN === 'shadow' ? 'shadow' : null

/** The current UTC month. */
export const thisMonth = (now = Date.now()): string => new Date(now).toISOString().slice(0, 7)

/** The digest converge job for ``month`` (default the current one: the scheduled run's), created or joined. */
export async function digestJob(env: DigestJobEnv, by: string, month?: string, now = Date.now()): Promise<SlackJob> {
  const mode = cfnMode(env)
  if (!mode) throw new Error('DIGEST_CFN is unset: no Worker converge on this deployment')
  const channel = env.SLACK_DIGEST_CHANNEL
  if (!channel) throw new Error('SLACK_DIGEST_CHANNEL is unset')
  const m = month ?? thisMonth(now)
  const args: DigestArgs = { month: m, current: m === thisMonth(now) }
  return createOrJoin(env.DB!, { kind: 'digest-converge', key: `${mode === 'shadow' ? 'shadow:' : ''}digest:${channel}/${m}`, args, shadow: mode === 'shadow', by }, now)
}

/** What a digest step reads from outside: the plot's renderer, the time. */
export interface DigestStepDeps {
  plot(req: Record<string, unknown>): Promise<string | null>
  now(): number
  log?(msg: string): void
}

/** One step of job ``id``: leased, run, released. Returns the job after it (unchanged when it couldn't be leased). */
export async function step(env: DigestJobEnv, id: string, deps: DigestStepDeps): Promise<SlackJob | null> {
  const db = env.DB!
  const t0 = deps.now()
  const job = await lease(db, id, t0)
  if (!job) return getJob(db, id)
  const memo = JSON.parse(job.memo || '{}') as JobMemo
  const budget = new Budget(Number(env.SLACK_STEP_CALLS) || DEFAULT_STEP_CALLS, t0 + STEP_WALL_MS, deps.now)
  try {
    const report = await digestStep(env, job, memo, budget, deps)
    return release(db, id, { state: 'done', memo, report, calls: budget.calls }, deps.now())
  } catch (e) {
    if (e instanceof OutOfBudget) return release(db, id, { state: 'running', memo, calls: budget.calls, error: e.message }, deps.now())
    if (e instanceof SlackApiError && e.code === 'ratelimited') {
      const wait = (e.retryAfterS ?? 30) * 1000
      return release(db, id, { state: 'running', memo, calls: budget.calls, retryAt: deps.now() + wait, error: `Slack rate limit: back in ${wait / 1000} s` }, deps.now())
    }
    if (e instanceof Busy) return release(db, id, { state: 'running', memo, calls: budget.calls, retryAt: deps.now() + 60_000, error: e.message }, deps.now())
    return release(db, id, { state: 'failed', memo, calls: budget.calls, error: e instanceof Error ? e.message : String(e) }, deps.now())
  }
}

// ---- the thread lease, as the job's (`digest_switch.thread_lease` → `append_runner.CoordinatorLease`) --------------

export class Busy extends Error {}
const LEASE_TTL_S = 900

interface LeaseRecord { owner: string; holder: string; at: string; since: string | null; ttl_s: number; region?: string }

/** Take ``month``'s thread lease (the record the job's `coordinator_lease` keeps), so a Worker's converge and a job's
 *  never run on one thread at once. Returns its release; throws `Busy` while another holds it. A stale record (past its
 *  `ttl_s`) is taken over; a release leaves a stale record behind (R2 can't delete conditionally), which the next taker,
 *  the job's included, takes over at once. */
export async function threadLease(store: SnapshotStore, key: string, holder: string, now: () => number): Promise<() => Promise<void>> {
  const owner = crypto.randomUUID().replaceAll('-', '')
  const at = () => new Date(now()).toISOString()
  const since = at()
  const record = (): LeaseRecord => ({ owner, holder, at: at(), since, ttl_s: LEASE_TTL_S })
  let version: string | null = null
  for (let i = 0; i < 3 && version === null; i++) {
    version = await store.putJson(key, record(), null)
    if (version !== null) break
    const cur = await store.getJson<LeaseRecord>(key)
    if (!cur) continue
    const age = (now() - Date.parse(cur.value.at)) / 1000
    if (age <= (cur.value.ttl_s ?? LEASE_TTL_S)) throw new Busy(`the thread lease is held by ${cur.value.holder ?? cur.value.owner} (since ${cur.value.since ?? cur.value.at})`)
    version = await store.putJson(key, record(), cur.version)
  }
  if (version === null) throw new Busy('the thread lease changed hands while being taken')
  const held = version
  return async () => {
    await store.putJson(key, { ...record(), at: new Date(0).toISOString(), ttl_s: 0 }, held)
  }
}

// ---- the digest step -------------------------------------------------------------------------------------------------

interface ThreadRow { variant: string; deletes: string; edits: string | null; reorder: string | null }

async function digestStep(env: DigestJobEnv, job: SlackJob, memo: JobMemo, budget: Budget, deps: DigestStepDeps): Promise<unknown> {
  const c = convergeConfig(env.DIGEST_CONFIG)
  const missing = unsupported(c)
  if (missing.length) throw new Error(`a Worker doesn't converge this digest yet: it uses ${missing.join(', ')}`)
  if (!env.SLACK_BOT_TOKEN || !env.SLACK_DIGEST_CHANNEL || !env.DB) throw new Error('needs SLACK_BOT_TOKEN, SLACK_DIGEST_CHANNEL and DB')
  const db = env.DB
  const channel = env.SLACK_DIGEST_CHANNEL
  const { month, current } = JSON.parse(job.args) as DigestArgs
  const shadow = job.shadow ? { writes: [] as ShadowWrite[], states: new Map<string, ThreadState>() } : undefined
  const store = storeFor(env, c.root)
  const slack = new SlackClient({ token: env.SLACK_BOT_TOKEN, channel, maxRetryWaitMs: MAX_WAIT_MS, beforeCall: budget.check })
  const log = deps.log ?? ((m: string) => console.log(m))

  const release = shadow ? null : await threadLease(store, leaseKey(c, channel, month), `site:${job.id}`, deps.now)
  try {
    const settings = await db.prepare('SELECT params, quota_marks, variant FROM digest_settings WHERE template = ?').bind(c.template)
      .first<{ params: string; quota_marks: string; variant: string | null }>().catch(() => null)
    const row = await db.prepare('SELECT variant, deletes, edits, reorder FROM digest_threads WHERE channel = ? AND month = ?').bind(channel, month)
      .first<ThreadRow>().catch(() => null)
    const runs = (await db.prepare("SELECT scan, trigger, started_ts FROM scan_runs WHERE store = 'primary' AND kind = 'scan'").all<{ scan: string; trigger: string | null; started_ts: number | null }>()).results
    const have = await workspaceCustom(slack, 'digest')
    const conv = new Converger(c, {
      store, slack, channel, have, settings, triggers: scanTriggers(runs), memo, shadow, log, now: () => new Date(deps.now()),
      plot: shadow ? async () => `${c.cfg.site_url}/a/shadow.png` : deps.plot,
    })

    // The month before: a thread a Worker can't close (its provisional reply, its quota alert) is the job's.
    const prev = await threadStates(store, c, channel, priorMonth(month))
    const prevLive = liveOf(prev)
    const ps = prevLive ? prev.get(prevLive)!.value : null
    if (ps && (Object.keys(ps.provisional ?? {}).length || ps.alert)) throw new Error(`${priorMonth(month)}'s thread holds a provisional reply or a quota alert, which a Worker doesn't close yet`)

    const states = await threadStates(store, c, channel, month)
    const live = liveOf(states)
    const desired = desiredVariant(row, live, settings?.variant ?? null, c.variant)
    if (live && live !== desired) throw new Error(`${month}'s thread is ${live}, wanted ${desired}: a switch, which a Worker doesn't run yet`)
    if ([...states.values()].some(s => s.value.retired_for && !s.value.retired_at)) throw new Error(`${month}'s thread has a retiring style, which a Worker doesn't finish yet`)
    const st = await conv.converge(month, desired, current)
    if (!shadow) return { month, variant: desired, op_ts: st?.value.op_ts ?? null }
    return { month, variant: desired, writes: shadow.writes.map(summarize), audit: await audit(conv, c, slack, channel, month, desired, store) }
  } finally {
    await release?.()
  }
}

/** Slack's stored text unescaped (it keeps `&`, `<` and `>` as entities outside its own link syntax). */
const unescape = (t: string | undefined): string | undefined => t?.replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&')

/** A shadow write, short: its method, the message it touches, its text. */
function summarize(w: ShadowWrite): Record<string, unknown> {
  const b = w.body
  return { method: w.method, ts: b['ts'] ?? b['thread_ts'] ?? null, text: typeof b['text'] === 'string' ? (b['text'] as string).slice(0, 400) : undefined }
}

export interface AuditLine { what: string; ts: string; same: boolean; live?: string; want?: string }

/** The shadow's audit: each message the thread's state names (the OP, each reply) as Slack holds it, against what this
 *  converge renders for it, wire text to wire text (`postPayload`: as the job's thrds sends it, the workspace's emoji
 *  fallback applied). An image block's URL isn't compared: the job's plot is hosted elsewhere. */
async function audit(conv: Converger, c: ConvergeConfig, slack: SlackClient, channel: string, month: string, variant: string, store: SnapshotStore): Promise<AuditLine[]> {
  const st = (await store.getJson<ThreadState>(stateKey(c, channel, variant, month)))?.value
  if (!st?.op_ts) return []
  const thread: Record<string, string> = {}
  let cursor: string | undefined
  do {
    const r = await slack.api('conversations.replies', { channel, ts: st.op_ts, limit: 200, ...(cursor ? { cursor } : {}) }, { get: true })
    for (const m of (r['messages'] ?? []) as { ts: string; text?: string }[]) thread[m.ts] = m.text ?? ''
    cursor = (r['response_metadata'] as { next_cursor?: string } | undefined)?.next_cursor || undefined
  } while (cursor)
  const rendered = conv.rendered
  const out: AuditLine[] = []
  const line = (what: string, ts: string, body: string | undefined) => {
    if (body === undefined) return
    const want = postPayload(channel, fallback(body, conv.d.have), { bust: '0' })['text'] as string
    const live = unescape(thread[ts])
    out.push(live === want ? { what, ts, same: true } : { what, ts, same: false, live: live?.slice(0, 600), want: want.slice(0, 600) })
  }
  line('op', st.op_ts, rendered.op)
  for (const [key, p] of Object.entries(st.posted ?? {})) line(key, typeof p === 'string' ? p : p.ts, rendered.replies[key])
  return out
}

/** A job as a route answers it: its row, `args` / `memo` / `report` parsed, `done` once it can't go on. */
export function jobView(j: SlackJob): Record<string, unknown> {
  return {
    id: j.id, kind: j.kind, key: j.key, args: JSON.parse(j.args), state: j.state, shadow: !!j.shadow, done: j.state === 'done' || j.state === 'failed',
    report: j.report ? JSON.parse(j.report) : null, retry_at: j.retry_at, calls: j.calls, steps: j.steps, error: j.error,
    created_by: j.created_by, created_at: j.created_at, updated_at: j.updated_at,
  }
}
