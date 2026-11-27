// `/staged`'s card counts what the Slack parent counts (`countedItems`): the
// queued prefixes less those empty at the scan (and those a real run deleted).
import { describe, expect, it, vi } from 'vitest'
import type { PrefixStat } from '../prefixes.js'
import { RUN_DIR, sqliteD1 } from '../testD1.js'

const SCAN = '2026-10-08'
const TiB = 2 ** 40
// Every prefix holds 1 TiB / 10 objects owned by `ann-bee`, except those under `empty/` (nothing) and under `cw/`
// (unowned: a store without attribution; `big/` there holds 3 TiB).
vi.mock('../index.js', async orig => ({
  ...(await orig<typeof import('../index.js')>()),
  storeReady: () => true,
  pathScans: async () => ({ results: [{ date: SCAN }] }),
}))
vi.mock('../prefixes.js', async orig => ({
  ...(await orig<typeof import('../prefixes.js')>()),
  prefixesAt: async (_env: unknown, _date: string, prefixes: string[]) => {
    const stats: Record<string, PrefixStat> = {}
    for (const p of prefixes) {
      if (p.includes('/empty/')) continue
      stats[p] = p.includes('/cw/') ? { bytes: (p.includes('/big/') ? 3 : 1) * TiB, objects: 10 } : { bytes: TiB, objects: 10, owners: [['ann-bee', TiB]] }
    }
    return { stats, groups: 1 }
  },
}))
vi.mock('../identity.js', async orig => ({
  ...(await orig<typeof import('../identity.js')>()),
  loadRegistry: async () => ({ 'ann-bee': { name: 'Ann Bee' } }),
}))
vi.mock('../ledger.js', () => ({ loadLedger: async () => ({ ownerRows: [] }) }))
vi.mock('../ownerTotals.js', () => ({ ownerTotals: async () => ({ users: { 'ann-bee': { b: TiB } } }) }))

const { stagedCard } = await import('./pages.js')
const { cardSvg } = await import('./card.js')
const { HI_CONTRAST } = await import('../../../src/colors.js')
const { UNOWNED_COLOR } = await import('./data.js')
const { DELETED_COLOR, STAGED_COLOR } = await import('./pages.js')
const textNodes = (svg: string) => [...svg.matchAll(/<text\b[^>]*>(.*?)<\/text>/g)].map(m => m[1])

describe('stagedCard', () => {
  it('counts the prefixes the Slack parent counts: those empty at the scan are left out', async () => {
    const { db } = await sqliteD1()
    await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann', 1)").run()
    for (const p of ['gs://b/x/', 'gs://b/y/', 'gs://b/empty/a/', 'gs://b/empty/b/']) {
      await db.prepare("INSERT INTO plan_items (plan_id, prefix, added_by, added_ts) VALUES (1, ?, 'ann', 1)").bind(p).run()
    }
    const card = await stagedCard({ DB: db } as never, { name: 'gcs', scheme: 'gs' }, 'Staged', {}, 'full')
    expect([card.subtitle, card.total, card.tiles.map(t => [t.name, t.b, t.kids?.map(k => k.name)])]).toEqual([
      `plan #1 · scan ${SCAN}`,
      '2 prefixes · 2.00 TiB · 20 objects',
      [['b', 2 * TiB, ['x/', 'y/']]],
    ])
  })

  it('a lone item keeps its own name: only dirs above it join the bucket\'s label', async () => {
    const { db } = await sqliteD1()
    await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann', 1)").run()
    for (const p of ['gs://b/x/y/z/', 'gs://c/k/', 'gs://c/k/m/', 'gs://d/top.bin']) {
      await db.prepare("INSERT INTO plan_items (plan_id, prefix, added_by, added_ts) VALUES (1, ?, 'ann', 1)").bind(p).run()
    }
    const card = await stagedCard({ DB: db } as never, { name: 'gcs', scheme: 'gs' }, 'Staged', {}, 'full')
    expect(card.tiles.map(t => [t.name, t.kids?.map(k => k.name)])).toEqual([
      ['b/x/y', ['z/']],
      ['c', ['k/', 'k/m/']],
      ['d', ['top.bin']],
    ])
  })

  it('a bucket whose items part into a few groups (one holding several): a tile each, its shared dirs heading it after `…`, so each item keeps the name that tells it apart', async () => {
    const { db } = await sqliteD1()
    await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann', 1)").run()
    for (const p of ['gs://b/runs/r1/ckpt/s1/', 'gs://b/runs/r1/ckpt/s2/', 'gs://b/runs/r2/ckpt/s1/']) {
      await db.prepare("INSERT INTO plan_items (plan_id, prefix, added_by, added_ts) VALUES (1, ?, 'ann', 1)").bind(p).run()
    }
    const card = await stagedCard({ DB: db } as never, { name: 'gcs', scheme: 'gs' }, 'Staged', {}, 'full')
    expect(card.tiles.map(t => [t.name, t.b, t.kids?.map(k => k.name)]).sort()).toEqual([
      ['b/…/r1/ckpt', 2 * TiB, ['s1/', 's2/']],
      ['b/…/r2/ckpt', TiB, ['s1/']],
    ])
  })

  it('unowned items (no attribution): each its own colour and label inside its bucket, and no owner legend; their shared dirs join the bucket\'s label', async () => {
    const { db } = await sqliteD1()
    await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann', 1)").run()
    for (const p of ['s3://b/cw/big/', 's3://b/cw/small/']) {
      await db.prepare("INSERT INTO plan_items (plan_id, prefix, added_by, added_ts) VALUES (1, ?, 'ann', 1)").bind(p).run()
    }
    const card = await stagedCard({ DB: db } as never, { name: 'cw', scheme: 's3' }, 'Staged', {}, 'full')
    expect([card.tiles.map(t => [t.name, t.color, t.kids?.map(k => [k.name, k.color])]), card.legend]).toEqual([
      [['b/cw', UNOWNED_COLOR, [['big/', HI_CONTRAST[0]], ['small/', HI_CONTRAST[1]]]]],
      [],
    ])
    expect(textNodes(cardSvg(card))).toEqual(['cw', 'Staged', `plan #1 · scan ${SCAN}`, 'big/ 3.00 TiB', 'small/ 1.00 TiB', 'b/cw 4.00 TiB', '2 prefixes · 4.00 TiB · 20 objects'])
  })

  /** Plan 1 whose thread (posted at 100) a real run since (started 200) deleted `s3://b/cw/big/` from (3 TiB, 30 objects;
   *  a real run unstages what it deleted), with `live` still staged. */
  async function threadPlan(live: string[]) {
    const { db, raw } = await sqliteD1()
    await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts, slack_channel, slack_ts) VALUES (1, 'Staged', 'open', 'ann', 1, 'C', '100.000001')").run()
    for (const p of live) await db.prepare("INSERT INTO plan_items (plan_id, prefix, added_by, added_ts) VALUES (1, ?, 'ann', 150)").bind(p).run()
    raw.prepare(`INSERT INTO deletion_runs (run_id, ${RUN_DIR}, scan, head, exec_head, actor, mode, started_ts, finished_ts, plan_digest, log_dir, plan_id)
      VALUES ('r1', 'p', ?, 0, 0, 'ann', 'real', 200, 300, 'D', 'd', 1)`).run(SCAN)
    raw.prepare("INSERT INTO deletion_bands (run_id, prefix, bytes, objects) VALUES ('r1', 's3://b/cw/big/', ?, 30)").run(3 * TiB)
    return db
  }

  it('once the thread deleted some: everything it staged, deleted ones sized by what was deleted, coloured by state, with a state legend', async () => {
    const card = await stagedCard({ DB: await threadPlan(['s3://b/cw/small/']) } as never, { name: 'cw', scheme: 's3' }, 'Staged', {}, 'full')
    expect([card.subtitle, card.total, card.tiles.map(t => [t.name, t.b, t.color, t.kids?.map(k => [k.name, k.b, k.color])]), card.legend, card.empty]).toEqual([
      `plan #1 · scan ${SCAN}`,
      '2 prefixes · 4.00 TiB · 40 objects · 3.00 TiB deleted, 1.00 TiB left',
      [['b/cw', 4 * TiB, UNOWNED_COLOR, [['big/', 3 * TiB, DELETED_COLOR], ['small/', TiB, STAGED_COLOR]]]],
      [{ label: 'deleted', color: DELETED_COLOR, b: 3 * TiB }, { label: 'still staged', color: STAGED_COLOR, b: TiB }],
      undefined,
    ])
  })

  it('everything the thread staged deleted: the card still maps it (never "nothing left"), all deleted', async () => {
    const card = await stagedCard({ DB: await threadPlan(['s3://b/cw/empty/x/']) } as never, { name: 'cw', scheme: 's3' }, 'Staged', {}, 'full')
    expect([card.subtitle, card.total, card.tiles.map(t => [t.name, t.color, t.kids?.map(k => [k.name, k.color])]), card.legend, card.empty]).toEqual([
      `plan #1 · scan ${SCAN}`,
      '1 prefixes · 3.00 TiB · 30 objects · all deleted',
      [['b/cw', DELETED_COLOR, [['big/', DELETED_COLOR]]]],
      [{ label: 'deleted', color: DELETED_COLOR, b: 3 * TiB }],
      undefined,
    ])
  })

  it('a closed thread\'s card (`th`): only its window\'s runs and the items staged before it ended', async () => {
    const db = await threadPlan(['s3://b/cw/small/'])
    // the thread ended at 250: the run (started 200) is in it, an item staged after it isn't
    await db.prepare("INSERT INTO plan_items (plan_id, prefix, added_by, added_ts) VALUES (1, 's3://b/cw/later/', 'ann', 260)").run()
    const card = await stagedCard({ DB: db } as never, { name: 'cw', scheme: 's3' }, 'Staged', { th: '100-250' }, 'full')
    const early = await stagedCard({ DB: db } as never, { name: 'cw', scheme: 's3' }, 'Staged', { th: '100-150' }, 'full')
    expect([card.total, early.total]).toEqual([
      '2 prefixes · 4.00 TiB · 40 objects · 3.00 TiB deleted, 1.00 TiB left',
      // ended before the run (and before `small/` was staged): nothing of it
      '',
    ])
  })
})
