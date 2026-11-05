/** What an isolate's map and diff reads may hold at once, admitted whole and in order.
 *
 * Requests in one isolate share its memory, and one past the limit takes every other request in the isolate down with
 * it (an uncatchable 1102). So a read asks first: its plan's whole charge (`planCharge`), once, before any of its data
 * is fetched. Past the cap it waits, oldest first; it never holds a charge while waiting for more, so two reads can't
 * each hold part of the cap and wait for the other. A plan bigger than the cap runs alone, once nothing else runs; the
 * queue's order keeps anything from starving it. A wait's end is a retryable 503 (`ReadsBusy`), never a coarser answer:
 * what a request reads depends on its plan, not on what else the isolate is doing, so cached answers don't depend on
 * traffic.
 *
 * A wait is built on the waiter's own timer, not on a promise another request resolves: Workers freeze a cancelled
 * request's pending promises, and a request that only awaits another's is cut off as hung (`shared.ts`). So a waiter
 * polls (`pollMs`), and a request whose timers stopped firing has left: a waiter that hasn't polled in `staleMs` is
 * dropped from the queue, a holder that hasn't beaten in `leaseMs` gives its charge back. A client's disconnect
 * (`request.signal`) does both at once. All of this needs the isolate's event loop: a decode blocks it for the length
 * of a row group (~100 ms), so timers run late by about that, and an isolate that dies takes its queue with it. */
import type { Trace } from './shared.js'
import { reqOf } from './shared.js'

/** Bytes a plan's row adds to a read's peak: the slope of the peak over the plan's distinct rows, on a real Worker
 *  (`bench/README.md`: each peak is the kill line less the most ballast the read survives beside it), ~60 B colo-warm.
 *  Far below a row's own size: a plan reads whole groups and keeps ~1% of them, so the rest is a group's doc on its way
 *  through the LRU. Per request: a group two requests read is counted for both, though it's decoded and held once. */
export const READ_ROW_HEAP = 56
/** What a read costs at any plan (its fetched runs, the groups being decoded): set so that a charge plus the caches a
 *  cleared isolate's read fills, which `READ_CAP.bytes` leaves room for, covers the measured colo-cold peaks. */
export const READ_REQUEST_HEAP = 32 * 2 ** 20

/** A read of at most this many rows goes in without asking: a request's point reads (a root's row, a level's lookups
 *  when it's at the root's scale), a group or so per tier. They are few and mostly the same groups for every request
 *  in the isolate (joined in flight, then held), and queueing them behind a whole plan makes a request wait in the line
 *  twice, before it knows its plan and again after. */
export const READ_SMALL_ROWS = 64_000

/** The cap and the wait's bounds (a knob object: tests shrink them). `bytes`: where a Worker dies, measured over the
 *  isolate's code (231 MiB of ballast lives, 246 doesn't, and 220 sometimes doesn't on an isolate that just held as much:
 *  past the 128 MiB Cloudflare documents; `bench/README.md`), less
 *  what the isolate can hold apart from reads, each at its bound: the group (24), footer (12) and level-index (8) LRUs and
 *  the static answers (32). */
export const READ_CAP = {
  bytes: (231 - 24 - 12 - 8 - 32) * 2 ** 20,
  /** How long a read waits before its 503. */
  waitMs: 30_000,
  /** Reads waiting at most: past this a 503 at once. Each holds its plan (tens of KB) and little else. */
  maxWaiters: 32,
  pollMs: 25,
  /** A waiter not seen for this long has left (its timers stopped: a cancelled request's do). */
  staleMs: 5_000,
  beatMs: 1_000,
  /** A holder not heard from for this long has left. Past a row group's decode, by far: the clock only advances across
   *  I/O on a Worker. */
  leaseMs: 15_000,
  /** A charge's longest life, heartbeat or not: nothing a Worker's CPU limit lets run holds its rows this long. */
  holdMs: 120_000,
}

/** The isolate has no room within a read's wait: a 503, never cached (it's thrown before the answer exists), that the
 *  client retries with backoff (`ivRefusal`). */
export class ReadsBusy extends Error {
  readonly retryAfter = 3
  constructor(why: string) {
    super(`reads busy: ${why}, retry`)
  }
}

/** A request's charge, once admitted. */
export interface Ticket {
  readonly bytes: number
  release(): void
}

interface Holder extends Ticket {
  at: number
  beat: number
}

interface Waiter {
  bytes: number
  seen: number
}

const sleep = (ms: number): Promise<void> => new Promise(r => setTimeout(r, ms))

/** The queue and the holders. Synchronous between polls, so two reads can't both take the last of the cap. */
export class ReadGate {
  private holders = new Set<Holder>()
  private queue: Waiter[] = []
  /** Bytes charged elsewhere that count against the cap (`chargeLevel`'s). */
  held: () => number = () => 0
  constructor(private readonly o: typeof READ_CAP = READ_CAP) {}

  /** Holders whose request has left (`leaseMs`, `holdMs`) give back. */
  private prune(now: number): void {
    for (const h of this.holders) {
      if (now - h.beat > this.o.leaseMs || now - h.at > this.o.holdMs) h.release()
    }
    while (this.queue.length && now - this.queue[0].seen > this.o.staleMs) this.queue.shift()
  }

  /** Bytes the isolate's reads hold now. */
  bytes(): number {
    this.prune(Date.now())
    return this.ownBytes()
  }

  private ownBytes(): number {
    let n = 0
    for (const h of this.holders) n += h.bytes
    return n
  }

  /** Reads waiting now. */
  get waiting(): number {
    return this.queue.length
  }

  /** Over the cap, a read fits only alone: with nothing else running at all. */
  private fits(bytes: number): boolean {
    const others = this.ownBytes() + this.held()
    return others === 0 || others + bytes <= this.o.bytes
  }

  private grant(bytes: number, signal?: AbortSignal | null): Ticket {
    const now = Date.now()
    let timer: ReturnType<typeof setInterval> | null = null
    const onAbort = () => h.release()
    const h: Holder = {
      bytes,
      at: now,
      beat: now,
      release: () => {
        if (!this.holders.delete(h)) return
        if (timer != null) clearInterval(timer)
        signal?.removeEventListener('abort', onAbort)
      },
    }
    this.holders.add(h)
    timer = setInterval(() => { h.beat = Date.now() }, this.o.beatMs)
    signal?.addEventListener('abort', onAbort, { once: true })
    return h
  }

  /** `bytes`' ticket, once it's this read's turn and they fit. Throws `ReadsBusy` when `maxWaiters` already wait or
   *  `waitMs` runs out. A gone client's wait ends at once with `ReadsBusy` (nobody reads it). */
  async admit(bytes: number, signal?: AbortSignal | null): Promise<Ticket> {
    const start = Date.now()
    this.prune(start)
    if (signal?.aborted) throw new ReadsBusy('client gone')
    if (!this.queue.length && this.fits(bytes)) return this.grant(bytes, signal)
    if (this.queue.length >= this.o.maxWaiters) throw new ReadsBusy(`${this.queue.length} reads waiting`)
    const me: Waiter = { bytes, seen: start }
    this.queue.push(me)
    const leave = () => {
      const i = this.queue.indexOf(me)
      if (i >= 0) this.queue.splice(i, 1)
    }
    signal?.addEventListener('abort', leave, { once: true })
    try {
      for (;;) {
        await sleep(this.o.pollMs)
        const now = Date.now()
        me.seen = now
        this.prune(now)
        if (signal?.aborted) throw new ReadsBusy('client gone')
        // Pruned (its timers ran so late it looked gone: an isolate blocked for seconds): back at the end of the line.
        if (!this.queue.includes(me)) this.queue.push(me)
        if (this.queue[0] === me && this.fits(bytes)) {
          this.queue.shift()
          return this.grant(bytes, signal)
        }
        if (now - start >= this.o.waitMs) throw new ReadsBusy(`no room for ${Math.round(bytes / 2 ** 20)} MiB in ${this.o.waitMs} ms`)
      }
    } finally {
      leave()
      signal?.removeEventListener('abort', leave)
    }
  }
}

/** The isolate's gate. */
export const readGate = new ReadGate()
/** A gate that admits at once and counts nothing. */
const UNCOUNTED = { admit: async (bytes: number) => ({ bytes, release: () => {} }), bytes: () => 0 } as unknown as ReadGate

/** A plan's groups, named across sorts and tiers: both sides of a diff read one group once. */
export interface PlanGroup {
  key: string
  rows: number
}

/** A plan's bytes: its distinct groups' rows, and what any request's reads cost. */
export const planCharge = (groups: readonly PlanGroup[]): number => {
  const rows = new Map(groups.map(g => [g.key, g.rows]))
  let n = 0
  for (const r of rows.values()) n += r
  return READ_REQUEST_HEAP + n * READ_ROW_HEAP
}

/** A request's reads as one charge: its plan's, taken once (`admit`, before any of the plan is fetched) and held to the
 *  end of its answer. */
export class Grant {
  private ticket?: Promise<Ticket>
  constructor(private readonly gate: ReadGate, private readonly signal?: AbortSignal | null) {}
  /** The charge. Asked again it's what was admitted, whatever `bytes` says: a request that holds never waits again, so
   *  two can't each hold part of the cap and wait for the other. */
  admit(bytes: number, trace?: Trace): Promise<Ticket> {
    return this.ticket ??= (async () => {
      const t0 = Date.now()
      const t = await this.gate.admit(bytes, this.signal)
      trace?.('rc-wait', Date.now() - t0)
      trace?.('rc-charge', Math.round(bytes / 2 ** 20))
      trace?.('rc-held', Math.round(this.gate.bytes() / 2 ** 20))
      return t
    })()
  }
  /** The charge if it was asked for: its reads wait only on it. */
  get held(): Promise<Ticket> | undefined {
    return this.ticket
  }
  release(): void {
    this.ticket?.then(t => t.release(), () => {})
  }
}

/** `f` as one request's reads (`env`'s), given back when `f` ends. What a route wraps its answer in, serialization
 *  included. Its plan takes the charge (`Grant.admit`); until then its reads are as `chargedRead` says. `background`:
 *  work that outlives its client (a partial's upgrade, under `waitUntil`), so a disconnect gives nothing back. */
export async function withReads<E extends object, T>(env: E, f: () => Promise<T>, { gate = readGate, background = false }: { gate?: ReadGate; background?: boolean } = {}): Promise<T> {
  const r = reqOf(env)
  if (!r) return f()
  const prev = r.reads
  const g = new Grant(gate, background ? undefined : r.signal)
  r.reads = g
  try {
    return await f()
  } finally {
    g.release()
    r.reads = prev
  }
}

/** `env`'s request already holds a charge elsewhere (`chargeLevel`): its reads never wait here, or it would hold that
 *  charge while it waits. */
export function chargedElsewhere(env: object): void {
  const r = reqOf(env)
  if (r && !r.reads?.held) {
    r.reads = new Grant(UNCOUNTED)
    void r.reads.admit(0)
  }
}

/** `f`, reading `groups`: under the request's charge once it holds one; a read of at most `READ_SMALL_ROWS` as is; any
 *  other under a charge of its own, for `f` alone, so it holds nothing while it waits. */
export async function chargedRead<T>(env: object, groups: readonly PlanGroup[], f: () => Promise<T>, gate: ReadGate = readGate): Promise<T> {
  const r = reqOf(env)
  const held = r?.reads?.held
  if (held) {
    await held
    return f()
  }
  if (groups.reduce((n, g) => n + g.rows, 0) <= READ_SMALL_ROWS) return f()
  const t = await gate.admit(planCharge(groups), r?.signal)
  try {
    return await f()
  } finally {
    t.release()
  }
}
