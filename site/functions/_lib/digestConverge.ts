/**
 * A month's digest thread converged from a Worker (specs/slack-in-workers.md, phase 1): `digest.converge_slack` and
 * the scheduled run around it (`close_previous`'s check, the thread lease, the variant a run converges), ported. It
 * reads and writes the job's own state JSON at the job's keys (`digestStore.ts`), renders with the site's renderers
 * (`src/digest`, parity-tested) and posts what the job's thrds client would (`digestWire.ts`), so the job and a Worker
 * converge one thread interchangeably.
 *
 * Every write is persisted as the job persists it (the state after each post), so a converge cut short (its Slack call
 * budget spent, a 429 longer than a step waits, an error) is resumed by converging again: done work is a no-op. What
 * a step must not repeat within one job (the OP's edit, the pin, the previous month's relink, the preview bundle) is
 * remembered in the job's ``memo``.
 *
 * Phase 1 converges what r2 uses: the `quota` template; the `sender`, `body` and `weekly` styles. A config using what
 * it doesn't port yet (the `cost` template, a provisional reply, the quota alert, the split reply, Discord) is refused
 * by name (`unsupported`), and that deployment keeps its Batch converge.
 */
import { apply, findIds, plan, type SlackClient, SlackApiError } from '@open-athena/thrds'
import { fallback, type Have } from '@disk-tree/pages-kit/emoji'
import { type DigestCfg, monthLinks, monthOf, opText, imageRequest, type ReplyCfg, replies, type QuotaRows } from '../../src/digest/index.js'
import { decodeParams } from '../../src/digest/codec.js'
import { MONTHS } from '../../src/digest/pyfmt.js'
import { loadMonth, parseRoot, type SnapshotStore } from './digestStore.js'
import { editPayload, postPayload, toSlack, type Sender } from './digestWire.js'

/** `digest.CONVERGE_CONFIG_VERSION`: keep identical. */
export const CONVERGE_CONFIG_VERSION = 1
/** `digest.PREVIEW_VERSION`. */
export const PREVIEW_VERSION = 1

/** `digest.converge_config`: the site's `DIGEST_CONFIG`. */
export interface ConvergeConfig {
  version: number
  template: 'quota' | 'cost'
  cfg: DigestCfg
  params: string
  reply: Omit<ReplyCfg, 'triggers'>
  root: string
  state: string
  preview: string | null
  variant: string
  variants: string[]
  edited_variants: string[]
  track_scan: boolean
  pin: boolean
  split_reply: boolean
  discord: boolean
  title: string
  op_sender: { icon_url?: string; icon_emoji?: string }
}

/** ``raw`` (`DIGEST_CONFIG`) parsed; throws on a missing var, bad JSON or another version. */
export function convergeConfig(raw: string | undefined): ConvergeConfig {
  if (!raw) throw new Error('DIGEST_CONFIG is unset (`dt-cloud digest-config` prints it)')
  const c = JSON.parse(raw) as ConvergeConfig
  if (c.version !== CONVERGE_CONFIG_VERSION) throw new Error(`DIGEST_CONFIG version ${c.version}: this site reads ${CONVERGE_CONFIG_VERSION}`)
  return c
}

/** What ``c`` uses that a Worker doesn't converge yet; empty: all of it. */
export function unsupported(c: ConvergeConfig): string[] {
  return [
    ...(c.template !== 'quota' ? [`the ${c.template} template`] : []),
    ...(c.reply.provisional ? ['provisional replies'] : []),
    ...(c.reply.alert_pct !== null ? ['the quota alert (alert_pct)'] : []),
    ...(c.split_reply ? ['the split reply'] : []),
    ...(c.discord ? ['the Discord twin'] : []),
  ]
}

/** A posted reply as the state records it: its ts, or (a tracked or edited style) `{ts, scan}`. */
export type Posted = string | { ts: string; scan: string }
/** A month's thread state, as the job writes it (`digest.load_state`): what this converge reads and writes of it. */
export interface ThreadState {
  op_ts?: string
  variant?: string
  pinned?: string
  posted?: Record<string, Posted>
  prev_url?: string | null
  next_url?: string | null
  plot_name?: string
  split_reply?: string
  provisional?: Record<string, { ts: string; scan: string }>
  alert?: { ts: string; body: string }
  retired_for?: string
  retired_at?: string
  switching_from?: string
  stale?: string[]
  [k: string]: unknown
}

export const tsOf = (p: Posted): string => (typeof p === 'string' ? p : p.ts)
const scanOf = (p: Posted): string | null => (typeof p === 'string' ? null : p.scan)

/** `YYYY-MM` arithmetic. */
export const priorMonth = (m: string): string => { const t = new Date(monthOf(m)); t.setUTCMonth(t.getUTCMonth() - 1); return t.toISOString().slice(0, 7) }
export const nextMonth = (m: string): string => { const t = new Date(monthOf(m)); t.setUTCMonth(t.getUTCMonth() + 1); return t.toISOString().slice(0, 7) }
const monthName = (m: string): string => `${MONTHS[Number(m.slice(5, 7)) - 1]} ${m.slice(0, 4)}`

/** The key of ``month``'s state for ``variant`` in ``channel`` (`digest.state_path`: beside the root's `snapshots/`). */
export function stateKey(c: ConvergeConfig, channel: string, variant: string, month: string): string {
  const { prefix } = parseRoot(c.root)
  const at = prefix.lastIndexOf('/snapshots')
  const base = at >= 0 ? prefix.slice(0, at) : prefix
  const dir = c.state.replaceAll('{channel}', channel).replaceAll('{variant}', variant)
  return [base, dir, `${month}.json`].filter(Boolean).join('/')
}

/** The thread lease's key (`digest_switch.lease_url`): the state dir without its variant, `<YYYY-MM>.lease.json`. */
export function leaseKey(c: ConvergeConfig, channel: string, month: string): string {
  const { prefix } = parseRoot(c.root)
  const at = prefix.lastIndexOf('/snapshots')
  const base = at >= 0 ? prefix.slice(0, at) : prefix
  const dir = c.state.replaceAll('{channel}', channel).replaceAll('{variant}', '').split('/').filter(Boolean).join('/')
  return [base, dir, `${month}.lease.json`].filter(Boolean).join('/')
}

export interface Versioned<T> { value: T; version: string | null }

/** ``month``'s states in ``channel`` by variant (`digest.thread_states`): only those that exist. */
export async function threadStates(store: SnapshotStore, c: ConvergeConfig, channel: string, month: string): Promise<Map<string, Versioned<ThreadState>>> {
  const out = new Map<string, Versioned<ThreadState>>()
  for (const v of c.variants) {
    const got = await store.getJson<ThreadState>(stateKey(c, channel, v, month))
    if (got && Object.keys(got.value).length) out.set(v, got)
  }
  return out
}

/** The live variant (`digest.live_of`): one with an OP that isn't retiring, minus one mid-switch from another live one.
 *  Two: refused, never guessed. */
export function liveOf(states: Map<string, Versioned<ThreadState>>): string | null {
  let live = [...states].filter(([, s]) => s.value.op_ts && !s.value.retired_for).map(([v]) => v)
  live = live.filter(v => !live.includes(states.get(v)!.value.switching_from ?? ''))
  if (live.length > 1) throw new Error(`digest: ${live.sort().join(' and ')} are both live; resolve the thread's state by hand`)
  return live[0] ?? null
}

/** The variant a run converges (`digest_switch.desired_variant`): the admin's row, else the live thread's, else the
 *  deployment's default (`digest_settings.variant`), else the config's. */
export const desiredVariant = (row: { variant: string } | null, live: string | null, dflt: string | null, file: string): string =>
  row?.variant || live || dflt || file

/** What survives a step within one job, so a resumed converge doesn't repeat a write it already made. */
export interface JobMemo {
  op?: string
  pinned?: boolean
  relinked?: boolean
  preview?: boolean
}

/** A Slack write a shadow run would have made: its method and body. */
export interface ShadowWrite { method: string; body: Record<string, unknown> }

export interface ConvergeDeps {
  store: SnapshotStore
  slack: SlackClient
  channel: string
  /** The workspace's custom emoji (`workspaceCustom`); a vocabulary emoji it lacks posts as its stock one. */
  have: Have
  /** The live settings row (`digest_settings`), when there is one. */
  settings: { params: string; quota_marks: string; variant: string | null } | null
  /** Each scan's trigger (`scan_triggers` over D1 `scan_runs`). */
  triggers: Record<string, string>
  /** The OP's plot: its public URL (drawn and stored), or null when there's none to draw. */
  plot(req: Record<string, unknown>): Promise<string | null>
  now(): Date
  memo: JobMemo
  /** A shadow run: Slack and state writes are recorded here, not made (reads still are). */
  shadow?: { writes: ShadowWrite[]; states: Map<string, ThreadState> }
  log(msg: string): void
}

/** ``c`` under the live settings row (`digest.with_settings`). */
function withSettings(c: ConvergeConfig, s: ConvergeDeps['settings']): { params: string; reply: Omit<ReplyCfg, 'triggers'> } {
  return s ? { params: s.params, reply: { ...c.reply, quota_marks: s.quota_marks as ReplyCfg['quota_marks'] } } : { params: c.params, reply: c.reply }
}

const benign = (e: unknown, ...codes: string[]): boolean => e instanceof SlackApiError && codes.includes(e.code)

/** The thread converger: one per step. */
export class Converger {
  private shadowTs = 0
  constructor(readonly c: ConvergeConfig, readonly d: ConvergeDeps) {}

  private async write(method: string, body: Record<string, unknown>): Promise<Record<string, unknown>> {
    if (this.d.shadow) {
      this.d.shadow.writes.push({ method, body })
      return method === 'chat.postMessage' ? { ts: `shadow.${++this.shadowTs}` } : {}
    }
    return this.d.slack.api(method, body)
  }

  private text(body: string): string { return fallback(body, this.d.have) }

  async post(body: string, o: { threadTs?: string; sender?: Sender; broadcast?: boolean } = {}): Promise<string> {
    const r = await this.write('chat.postMessage', postPayload(this.d.channel, this.text(body), o))
    return r['ts'] as string
  }
  async edit(ts: string, body: string): Promise<void> { await this.write('chat.update', editPayload(this.d.channel, ts, this.text(body))) }
  async delete(ts: string): Promise<void> {
    try { await this.write('chat.delete', { channel: this.d.channel, ts }) } catch (e) { if (!benign(e, 'message_not_found')) throw e }
  }
  async permalink(ts: string): Promise<string | null> {
    if (ts.startsWith('shadow.')) return null
    const r = await this.d.slack.api('chat.getPermalink', { channel: this.d.channel, message_ts: ts }, { get: true })
    return r['permalink'] as string
  }

  async load(key: string): Promise<Versioned<ThreadState>> {
    const s = this.d.shadow?.states.get(key)
    if (s) return { value: s, version: null }
    const got = await this.d.store.getJson<ThreadState>(key)
    return got ?? { value: {}, version: null }
  }
  /** ``st`` saved at ``key`` if no other writer moved it since it was read (``st.version``, updated). */
  async save(key: string, st: Versioned<ThreadState>): Promise<void> {
    if (this.d.shadow) { this.d.shadow.states.set(key, st.value); return }
    const v = await this.d.store.putJson(key, st.value, st.version)
    if (v === null) throw new Error(`${this.d.store.where}/${key}: changed by another writer while converging; the next step re-reads it`)
    st.version = v
  }

  /** The previous month's OP in the channel (its live state in any variant: a thread switched between styles links as
   *  one), or null. `digest._prev_op` asked only the run's own variant for the OP's link, so a month that changed style
   *  lost its `← <previous>` link; this asks all, as its pin does. */
  async prevOp(month: string): Promise<string | null> {
    const prev = await threadStates(this.d.store, this.c, this.d.channel, priorMonth(month))
    const live = liveOf(prev)
    return live ? prev.get(live)!.value.op_ts ?? null : null
  }

  /** `digest.pin_op`: pin the OP, unpin the previous month's; a failure is a warning, retried next run. */
  async pin(st: ThreadState, opTs: string, prevOp: string | null): Promise<boolean> {
    if (st.pinned === opTs) return false
    try {
      try {
        await this.write('pins.add', { channel: this.d.channel, timestamp: opTs })
      } catch (e) {
        if (benign(e, 'already_pinned')) { /* in place */ } else if (benign(e, 'not_in_channel')) {
          try { await this.write('conversations.join', { channel: this.d.channel }) } catch (j) { if (!benign(j, 'method_not_supported_for_channel_type')) throw j }
          try { await this.write('pins.add', { channel: this.d.channel, timestamp: opTs }) } catch (p) { if (!benign(p, 'already_pinned')) throw p }
        } else throw e
      }
      if (prevOp) {
        try { await this.write('pins.remove', { channel: this.d.channel, timestamp: prevOp }) } catch (e) { if (!benign(e, 'no_pin', 'message_not_found')) throw e }
      }
    } catch (e) {
      if (e instanceof SlackApiError && e.code !== 'ratelimited') {
        this.d.log(`digest: WARN could not pin the OP ${opTs}: ${e.code} (retried next run)`)
        return false
      }
      throw e
    }
    st.pinned = opTs
    this.d.log(`digest: pinned the OP ${opTs}${prevOp ? `, unpinned ${prevOp}` : ''}`)
    return true
  }

  /** `digest.link_prev`: the previous month's OP's month-link line rewritten to link this month's, in place (its text
   *  as Slack stores it, never re-rendered), its state recording the links. A failure is a warning. */
  async linkPrev(month: string): Promise<void> {
    const prev = priorMonth(month)
    try {
      await this.relink(prev)
    } catch (e) {
      if (e instanceof SlackApiError && e.code === 'ratelimited') throw e
      this.d.log(`digest: WARN could not link ${prev}'s OP to ${month}'s: ${(e as Error).message} (retried next run)`)
    }
  }

  private async opUrl(month: string): Promise<string | null> {
    const ts = await this.prevOpOf(month)
    return ts ? this.permalink(ts) : null
  }
  private async prevOpOf(month: string): Promise<string | null> {
    const states = await threadStates(this.d.store, this.c, this.d.channel, month)
    const live = liveOf(states)
    return live ? states.get(live)!.value.op_ts ?? null : null
  }

  /** `digest.relink` for one month. */
  async relink(month: string): Promise<void> {
    const states = await threadStates(this.d.store, this.c, this.d.channel, month)
    const live = liveOf(states)
    if (!live) return
    const key = stateKey(this.c, this.d.channel, live, month)
    const st = await this.load(key)
    const opTs = st.value.op_ts
    if (!opTs) return
    const [prevUrl, nextUrl] = [await this.opUrl(priorMonth(month)), await this.opUrl(nextMonth(month))]
    if ('prev_url' in st.value && 'next_url' in st.value && st.value.prev_url === prevUrl && st.value.next_url === nextUrl) return
    const line = toSlack(monthLinks(monthOf(month), { plotUrl: null, prevUrl, nextUrl }).at(-1) ?? '')
    if (this.d.shadow) {
      this.d.shadow.writes.push({ method: 'relink', body: { ts: opTs, line } })
      return
    }
    const p = await plan(await findIds(this.d.slack, [opTs]), f => relinkDesired(f.content, line))
    const outcomes = await apply(p, this.d.slack, { dryRun: false, paceMs: 0 })
    const shown = p.actions.some(a => a.ts === opTs && a.kind === 'noop') || outcomes.some(o => o.ts === opTs && (o.status === 'done' || o.status === 'already'))
    if (!shown) {
      const why = [...p.actions.filter(a => a.ts === opTs && a.reason).map(a => `${a.kind}: ${a.reason}`), ...outcomes.filter(o => o.ts === opTs).map(o => o.status)]
      this.d.log(`digest: WARN ${month}'s OP ${opTs} not relinked: ${why.join('; ') || 'not planned'}`)
      return
    }
    st.value.prev_url = prevUrl
    st.value.next_url = nextUrl
    await this.save(key, st)
    this.d.log(`digest: relinked ${month}'s OP ${opTs}`)
  }

  /** `digest.converge_slack` for ``month`` in ``variant``: the OP (posted or edited; with ``current``, pinned and the
   *  previous month linked to it), the preview bundle, then each reply posted, or edited once a later scan lands (an
   *  edited style). Returns the state, or null when the month has no scans. */
  async converge(month: string, variant: string, current: boolean, rows?: QuotaRows | null): Promise<Versioned<ThreadState> | null> {
    const c = this.c
    const data = rows === undefined ? await loadMonth(this.d.store, c.root, month, c.cfg.primary!, c.cfg.unit.bytes) : rows
    if (!data) { this.d.log(`digest: no scans for ${month}`); return null }
    const { params, reply } = withSettings(c, this.d.settings)
    const rc: ReplyCfg = { ...reply, triggers: triggersOf(data, this.d.triggers) }
    const key = stateKey(c, this.d.channel, variant, month)
    const st = await this.load(key)
    const s = st.value

    const plotUrl = await this.d.plot(imageRequest(data, month, c.cfg, decode(params)))
    const prevOp = await this.prevOp(month)
    const prevUrl = prevOp ? await this.permalink(prevOp) : null
    const body = opText(data, month, c.cfg, { plotUrl, prevUrl, nextUrl: s.next_url ?? null }, decode(params))
    if (c.variants.length > 1) s.variant = variant
    if (s.op_ts) {
      if (this.d.memo.op !== body) {
        await this.edit(s.op_ts, body)
        this.d.memo.op = body
        this.d.log(`digest: edited OP ${s.op_ts}`)
      }
    } else {
      s.op_ts = await this.post(body, { sender: { username: `${c.title} — ${monthName(month)}`, ...c.op_sender } })
      this.d.memo.op = body
      await this.save(key, st)
      this.d.log(`digest: posted OP ${s.op_ts}`)
    }
    const opTs = s.op_ts!
    if (current && c.pin && !this.d.memo.pinned) {
      if (await this.pin(s, opTs, prevOp)) await this.save(key, st)
      this.d.memo.pinned = true
    }
    if (current && !this.d.memo.relinked) {
      await this.save(key, st)
      await this.linkPrev(month)
      this.d.memo.relinked = true
    }
    if (!c.split_reply && s.split_reply) {
      await this.delete(s.split_reply)
      delete s.split_reply
      await this.save(key, st)
    }
    const units = replies(data, c.cfg, rc, variant).reverse()
    if (c.preview && !this.d.memo.preview) {
      await this.writePreview(month, variant, { data, params, rc, body, plotUrl, prevUrl, opTs, nextUrl: s.next_url ?? null, units })
      this.d.memo.preview = true
    }

    const posted = (s.posted ??= {})
    const record = (ts: string, scan: string): Posted => (c.track_scan || c.edited_variants.includes(variant) ? { ts, scan } : ts)
    for (const u of units) {
      const have = posted[u.key]
      if (have === undefined) {
        const ts = await this.post(u.body, { threadTs: opTs, sender: { username: u.username, icon_url: u.icon_url, icon_emoji: u.icon_emoji } })
        posted[u.key] = record(ts, u.scan)
        await this.save(key, st)
        this.d.log(`digest: reply ${u.key} (${u.scan}) -> ${ts}`)
      } else if (c.edited_variants.includes(variant) && scanOf(have) !== u.scan) {
        await this.edit(tsOf(have), u.body)
        if (typeof have !== 'string') have.scan = u.scan
        await this.save(key, st)
        this.d.log(`digest: edited reply ${u.key} -> ${u.scan}`)
      }
    }
    for (const [k, p] of Object.entries(s.provisional ?? {})) {
      await this.delete(p.ts)
      delete s.provisional![k]
      await this.save(key, st)
    }
    if (s.provisional && !Object.keys(s.provisional).length) delete s.provisional
    if (s.alert) throw new Error(`digest: ${month}'s state holds a quota alert, which a Worker doesn't converge yet; converge it with the job`)
    await this.save(key, st)
    return st
  }

  private async writePreview(month: string, variant: string, a: {
    data: QuotaRows; params: string; rc: ReplyCfg; body: string; plotUrl: string | null; prevUrl: string | null; opTs: string
    nextUrl: string | null; units: ReturnType<typeof replies>
  }): Promise<void> {
    const c = this.c
    const url = c.preview!.replaceAll('{channel}', this.d.channel).replaceAll('{variant}', variant).replace(/\/+$/, '')
    const { bucket, prefix } = parseRoot(`${url}/x`)
    if (`${parseRoot(c.root).scheme}://${bucket}` !== this.d.store.where) throw new Error(`digest preview ${url}: not in the digest's store ${this.d.store.where}`)
    const bundle = {
      version: PREVIEW_VERSION, month, channel: this.d.channel, variant, posted_at: this.d.now().toISOString().replace(/\.\d+Z$/, 'Z'),
      op_ts: a.opTs, permalink: await this.permalink(a.opTs), plot_url: a.plotUrl, prev_url: a.prevUrl, next_url: a.nextUrl,
      cfg: c.cfg, rows: a.data, params: a.params, op: a.body, image: imageRequest(a.data, month, c.cfg, decode(a.params)),
      replies: [...a.units].reverse().map(u => ({ key: u.key, scan: u.scan, username: u.username, body: u.body, icon_url: u.icon_url, icon_emoji: u.icon_emoji })),
      reply: a.rc,
    }
    const key = `${prefix.replace(/\/x$/, '')}/${month}.json`
    if (this.d.shadow) { this.d.shadow.writes.push({ method: 'preview', body: { key } }); return }
    const cur = await this.d.store.getJson<unknown>(key)
    await this.d.store.putJson(key, bundle, cur?.version ?? null)
  }
}

const decode = (p: string) => decodeParams(p)

/** The window's scans' triggers (`QuotaTemplate.reply_json`'s `triggers`): only those of the rows it renders. */
function triggersOf(rows: QuotaRows, all: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {}
  for (const r of [...rows.pre, ...rows.lead, ...rows.rows]) if (all[r.scan]) out[r.scan] = all[r.scan]
  return out
}

// ---- relink: the month-link line of an OP as Slack stores it (`digest.relink_text` / `relink_desired`) -----------

const WIRE_MONTH = String.raw`<[^|>\n]+\|(?:← [A-Z][a-z]+|[A-Z][a-z]+ →)>`
const WIRE_MONTHS_LINE = new RegExp(String.raw`^${WIRE_MONTH}(?: · ${WIRE_MONTH})*$`)
const WIRE_MONTHS_TAIL = new RegExp(String.raw`(?<= )${WIRE_MONTH}(?: · ${WIRE_MONTH})*$`)
const IMAGE_ADDED = new Set(['image_width', 'image_height', 'image_bytes', 'fallback', 'is_animated'])

/** ``wire`` with its month-link line replaced by ``line`` (`""` drops it and the blank line before it), else ``line``
 *  appended after a blank line; a text whose newlines Slack flattened has its links at the end of its last line. */
export function relinkText(wire: string, line: string): string {
  const lines = wire.split('\n')
  const at = lines.map((s, i) => (WIRE_MONTHS_LINE.test(s) ? i : -1)).filter(i => i >= 0)
  if (at.length) {
    const i = at[at.length - 1]
    if (line) lines[i] = line
    else {
      lines.splice(i, 1)
      if (i && !lines[i - 1]) lines.splice(i - 1, 1)
    }
    return lines.join('\n')
  }
  let last = -1
  lines.forEach((s, i) => { if (s.trim()) last = i })
  if (last >= 0) {
    const t = lines[last].trimEnd()
    const m = WIRE_MONTHS_TAIL.exec(t)
    if (m) {
      const head = t.slice(0, m.index)
      lines[last] = line ? `${head}${line}` : head.trimEnd()
      return lines.join('\n')
    }
  }
  return line ? `${wire.replace(/\n+$/, '')}\n\n${line}` : wire
}

type Block = Record<string, unknown>

/** The rewrite target for an OP as read back: its text and its first mrkdwn section relinked, other blocks as stored
 *  (an image's Slack-added fields dropped); an OP with only Slack's own rendering of its text is text only. */
export function relinkDesired(found: { text: string; blocks?: Block[] | null }, line: string): { text: string; blocks?: Block[] } {
  const text = relinkText(found.text, line)
  const blocks = found.blocks ?? []
  if (!blocks.length || blocks.every(b => b['type'] === 'rich_text')) return { text }
  let done = false
  return {
    text,
    blocks: blocks.map(b => {
      const t = b['text'] as { type?: string; text?: string } | undefined
      if (!done && b['type'] === 'section' && t?.type === 'mrkdwn') {
        done = true
        return { ...b, text: { ...t, text: relinkText(t.text ?? '', line) } }
      }
      if (b['type'] === 'image') return Object.fromEntries(Object.entries(b).filter(([k]) => !IMAGE_ADDED.has(k)))
      return b
    }),
  }
}
