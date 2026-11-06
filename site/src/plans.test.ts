import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MutationObserver, QueryClient, QueryClientProvider, type QueryObserverOptions } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { dispatchOptions, type DispatchVars, EXEC_JOBS_KEY, runActionOptions, usePrefixHistory } from './plans'
import { EXEC_CAPS, joinRuns, runControls, type DeletionRun, type ExecJob } from './runs'

vi.mock('./stores', () => ({ DEFAULT_STORE: { key: 'cw', executor: 'plan-sweep' } }))

it('loads CW per-prefix deletion outcomes even when its executor does not expose run files', () => {
  const client = new QueryClient()
  function History() { usePrefixHistory(2, false); return null }
  renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(History)))
  const options = client.getQueryCache().find({ queryKey: ['prefix-history', 2] })?.options as QueryObserverOptions | undefined
  expect(options?.enabled).toBe(true)
  client.clear()
})

describe('a run control (stop / undo / purge) and the jobs list its feedback comes from', () => {
  afterEach(() => vi.unstubAllGlobals())

  const RUN: DeletionRun = { run_id: 'r1', plan_id: 1, mode: 'real', started_ts: 100, finished_ts: 200, deleted_bytes: 1, deleted_objects: 1, skipped_gone: 0, undo_deadline: 900, undo_state: 'none' }
  const UNDO: ExecJob = { job_id: 'undo-1', op: 'undo', target: 'r1', state: 'QUEUED' }
  const NONE = { jobs: [] as ExecJob[], configured: true }
  /** A client that holds a list fresh for a minute, as the app's does (`main.tsx`): a read it would skip is the one that matters. */
  const appClient = () => new QueryClient({ defaultOptions: { queries: { staleTime: 60_000 } } })
  const deferred = <T,>() => { let resolve!: (v: T) => void; const promise = new Promise<T>(r => { resolve = r }); return { promise, resolve } }
  /** The undo control of `RUN`, by the jobs the page holds: `[offered, undoing]`. */
  const control = (client: QueryClient): [boolean, boolean] => {
    const c = runControls(joinRuns([RUN], client.getQueryData<typeof NONE>(EXEC_JOBS_KEY)!.jobs)[0], EXEC_CAPS['plan-sweep'], true, 300)
    return [c.undo, c.undoing]
  }
  /** A backend whose jobs list gains the undo's job when the undo is posted; each call as `[url, method, cache]`. `jobsGate`:
   *  what a jobs read waits for. */
  const backend = (jobsGate: Promise<unknown> = Promise.resolve(), jobsStatus = 200) => {
    const calls: [string, string, RequestCache | undefined][] = []
    let jobs: ExecJob[] = []
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      calls.push([url, init.method!, init.cache])
      if (init.method === 'POST') { jobs = [UNDO]; return new Response(JSON.stringify({ ok: true, job_id: UNDO.job_id })) }
      await jobsGate
      return new Response(JSON.stringify(jobsStatus === 200 ? { jobs, configured: true } : { error: 'batch list failed' }), { status: jobsStatus })
    })
    return calls
  }

  it('stays pending until the jobs list is read again past the browser\'s copy, so the control is never offered in between', async () => {
    const client = appClient()
    client.setQueryData(EXEC_JOBS_KEY, NONE)
    const gate = deferred<void>()
    const calls = backend(gate.promise)
    const action = new MutationObserver(client, runActionOptions(client))
    const done = action.mutate({ action: 'undo', run_id: 'r1' })
    await vi.waitFor(() => expect(calls).toEqual([['/api/plan-sweep/undo', 'POST', undefined], ['/api/plan-sweep/jobs', 'GET', 'reload']]))
    // The undo is submitted and the fresh list not in yet: still pending (the page's `busy`), on the list as it was.
    expect([action.getCurrentResult().isPending, control(client)]).toEqual([true, [true, false]])
    gate.resolve()
    await done
    expect([action.getCurrentResult().isPending, client.getQueryData(EXEC_JOBS_KEY), control(client)]).toEqual([false, { jobs: [UNDO], configured: true }, [false, true]])
    client.clear()
  })

  it('a jobs poll already in flight, answered from before the job existed, is not what the page ends on', async () => {
    const client = appClient()
    client.setQueryData(EXEC_JOBS_KEY, NONE)
    const stale = deferred<typeof NONE>()
    const poll = client.fetchQuery({ queryKey: EXEC_JOBS_KEY, queryFn: () => stale.promise, staleTime: 0 }).catch(() => undefined)
    const calls = backend()
    await new MutationObserver(client, runActionOptions(client)).mutate({ action: 'undo', run_id: 'r1' })
    // The poll's answer arrives after the fresh one: too late to count.
    stale.resolve(NONE)
    await poll
    expect([calls, client.getQueryData(EXEC_JOBS_KEY)]).toEqual([
      [['/api/plan-sweep/undo', 'POST', undefined], ['/api/plan-sweep/jobs', 'GET', 'reload']],
      { jobs: [UNDO], configured: true },
    ])
    client.clear()
  })

  it('a jobs list that can\'t be read fails the list, not the control that landed', async () => {
    const client = appClient()
    client.setQueryData(EXEC_JOBS_KEY, NONE)
    backend(Promise.resolve(), 500)
    const action = new MutationObserver(client, runActionOptions(client))
    await action.mutate({ action: 'undo', run_id: 'r1' })
    expect([action.getCurrentResult().status, client.getQueryState(EXEC_JOBS_KEY)?.error?.message]).toEqual(['success', 'batch list failed'])
    client.clear()
  })
})

// The guard at the call itself, whatever the page's controls did. `sized`: the scan the page's sizes were read at, so the
// one its checkable rows, its buckets, its machine and its `expected` come from; `date`: the scan the run reads.
describe('a dispatch goes out only from a page sized at the scan its run reads', () => {
  afterEach(() => vi.unstubAllGlobals())

  const NEW = '2026-11-05T0702', OLD = '2026-11-04T0703'
  const REAL: DispatchVars = { mode: 'real', date: NEW, sized: NEW, items: ['gs://b/x/', '=gs://b/y'], shownMinSoftDeleteDays: 7, expected: { bytes: 1000, objects: 10 } }
  const DRY: DispatchVars = { mode: 'dry', date: NEW, sized: NEW, items: ['gs://b/x/'], buckets: ['b'], machine: 'n2-highmem-8', expected: { bytes: 600, objects: 6 } }
  const SAID = 'Sizes are shown at scan 2026-11-04T0703. A run reads the newest scan, 2026-11-05T0702. Switch "sized at scan" back to 2026-11-05T0702 to dispatch.'
  /** `vars` dispatched on plan `planId`: every request made, as `[url, method, body]`, and the error the page shows. */
  const dispatched = async (planId: number | null, vars: DispatchVars) => {
    const calls: [string, string, unknown][] = []
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      calls.push([url, init.method!, init.body ? JSON.parse(init.body as string) : undefined])
      return new Response(JSON.stringify(init.method === 'POST' ? { job_id: 'job-1' } : { jobs: [], configured: true }))
    })
    const client = new QueryClient()
    const action = new MutationObserver(client, dispatchOptions(client, planId))
    await action.mutate(vars).catch(() => undefined)
    client.clear()
    return { posts: calls.filter(c => c[1] === 'POST'), error: action.getCurrentResult().error?.message ?? null }
  }

  it('sized at the scan the run reads: posted, real or dry, with the expected size read at that scan', async () => {
    expect([await dispatched(7, REAL), await dispatched(7, DRY)]).toEqual([
      { posts: [['/api/plan-sweep/dispatch', 'POST', { plan_id: 7, mode: 'real', date: NEW, items: ['gs://b/x/', '=gs://b/y'], shown_min_soft_delete_days: 7, expected: { bytes: 1000, objects: 10 } }]], error: null },
      { posts: [['/api/plan-sweep/dispatch', 'POST', { plan_id: 7, mode: 'dry', date: NEW, items: ['gs://b/x/'], buckets: ['b'], machine: 'n2-highmem-8', expected: { bytes: 600, objects: 6 } }]], error: null },
    ])
  })
  it('sized at an older scan: nothing is posted, real or dry, and the error is what the controls say', async () => {
    expect([await dispatched(7, { ...REAL, sized: OLD }), await dispatched(7, { ...DRY, sized: OLD })]).toEqual([
      { posts: [], error: SAID },
      { posts: [], error: SAID },
    ])
  })
  it('nothing staged, or nothing checked: nothing is posted, as before', async () => {
    expect([await dispatched(null, REAL), await dispatched(7, { ...REAL, items: [] })]).toEqual([
      { posts: [], error: 'nothing staged' },
      { posts: [], error: 'nothing selected' },
    ])
  })
})
