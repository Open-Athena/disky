import { expect, test, type Page } from '@playwright/test'

const FIXTURE = '/e2e/fixtures/session-log.html'
interface Event { k: string; u?: string; s?: number; err?: string; msg?: string }
async function setup(page: Page, diagnostics = false) {
  const now = Date.now()
  const posted: Event[] = []
  const filters: string[] = []
  const session = { sid: 'sid-actor1', email: null, via: 'grant', grant_id: 'grant-fixture', subject_name: 'Trusted recipient', build: 'fixture', ua: null, started_ms: now - 5000, last_ms: now, n_events: 2, n_errors: 0, n_batches: 1, first_url: '/', received_ts: Math.floor(now / 1000) }
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url())
    let json: unknown = {}
    if (url.pathname === '/api/session-log') {
      if (request.method() === 'POST') { posted.push(...JSON.parse(request.postData()!).events); await route.fulfill({ status: 204 }); return }
      json = { enabled: true, on: true, until: now + 86_400_000, reason: null, retainDays: 30, appEvents: {} }
    } else if (url.pathname === '/api/session-log/switch') json = { enabled: true, indefinite: false, until: now + 86_400_000, reason: null, who: 'ops@example.test', ts: Math.floor(now / 1000), defaultUntil: now + 7 * 86_400_000, maxUntil: now + 31 * 86_400_000 }
    else if (url.pathname === '/api/session-log/sessions') {
      filters.push(url.searchParams.get('email') ?? '')
      json = { sessions: [session], total: 1, offset: 0, prev: null, next: null }
    } else if (url.pathname === '/api/session-log/sessions/sid-actor1') json = { session, events: [
      { t: now - 5000, k: 'start', u: '/', pid: 'pid-fixture', seq: 0 },
      ...(diagnostics ? [
        { t: now - 4000, k: 'life', s: 'gap', ms: 48 * 60_000, on: true, vis: 'visible', pid: 'pid-fixture', seq: 0 },
        { t: now - 3000, k: 'api', id: 7, u: '/api/series?path=marin-na', s: 0, ms: 2400, end: now - 600, err: 'TypeError: Failed to fetch', on0: true, on: false, vis0: 'visible', vis: 'visible', visMs: 0, gapMs: 48 * 60_000, gapAgo: 0, pid: 'pid-fixture', seq: 0 },
        { t: now, k: 'api', id: 8, u: '/api/series?path=marin-na', s: 200, ms: 30, ray: '1234567890abcdef-EWR', pid: 'pid-fixture', seq: 0 },
      ] : [{ t: now, k: 'api', u: '/api/cancelled', s: 0, ms: 10, err: 'abort', pid: 'pid-fixture', seq: 0 }]),
    ] }
    await route.fulfill({ json })
  })
  await page.route('**/data/rules.json*', route => route.fulfill({ json: { users: [], rules: [] } }))
  await page.goto(FIXTURE)
  await page.waitForFunction(() => document.documentElement.dataset.sessionLogReady === 'true')
  return { posted, filters }
}

test('renders the app switch, trusted recipient, URL filters and shared session timeline', async ({ page }, info) => {
  const { filters } = await setup(page)
  await expect(page.getByRole('switch', { name: 'Logging' })).toBeChecked()
  await expect(page.getByText('Trusted recipient', { exact: true })).toHaveCount(1)
  await page.getByLabel('viewer', { exact: true }).fill('Trusted')
  await expect.poll(() => filters.at(-1)).toBe('Trusted')
  expect(new URL(page.url()).searchParams.get('u')).toBe('Trusted')
  await page.screenshot({ path: info.outputPath('list.png'), fullPage: true })
  await page.locator('a[href="/admin/sessions/sid-actor1"]').click()
  await expect(page.getByText('GET /api/cancelled → cancelled · 10ms', { exact: true })).toBeVisible()
  await page.screenshot({ path: info.outputPath('timeline.png'), fullPage: true })
})

test('renders connectivity evidence and the later successful read while retaining the failed attempt', async ({ page }, info) => {
  await setup(page, true)
  await page.locator('a[href="/admin/sessions/sid-actor1"]').click()
  await expect(page.getByText('browser gap 48m 00s · online hint · visible', { exact: true })).toBeVisible()
  await expect(page.getByText('GET /api/series?path=marin-na → failed · 2400ms · TypeError: Failed to fetch · request #7 · online hint → offline hint · visible · 0ms since visible · timer gap 48m 00s, 0ms before request', { exact: true })).toBeVisible()
  await expect(page.getByText('GET /api/series?path=marin-na → 200 · 30ms · request #8 · Ray 1234567890abcdef-EWR · later success after request #7 (600ms after failure)', { exact: true })).toBeVisible()
  await page.screenshot({ path: info.outputPath('network-context.png'), fullPage: true })
})

test('shared client ignores benign browser errors and distinguishes aborts, navigation and real failures', async ({ page }) => {
  await page.clock.install()
  const { posted } = await setup(page)
  await page.route('**/api/qa-leaving', route => route.abort('failed'))
  await page.route('**/api/qa-failed', route => route.abort('failed'))
  await page.evaluate(async () => {
    window.dispatchEvent(new ErrorEvent('error', { message: 'ResizeObserver loop completed with undelivered notifications.' }))
    window.dispatchEvent(new ErrorEvent('error', { message: 'real failure' }))
    const controller = new AbortController(); controller.abort()
    await fetch('/api/qa-aborted', { signal: controller.signal }).catch(() => {})
    await fetch('/api/qa-failed').catch(() => {})
  })
  await page.clock.fastForward(5001)
  await page.evaluate(async () => {
    await fetch('/api/qa-leaving').catch(() => {})
    window.dispatchEvent(new Event('pagehide'))
  })
  await expect.poll(() => posted.filter(event => event.k === 'error').map(event => event.msg)).toEqual(['real failure'])
  await expect.poll(() => posted.filter(event => event.k === 'api' && event.u?.startsWith('/api/qa-')).map(({ u, s, err }) => ({ u, s, err }))).toEqual([
    { u: '/api/qa-aborted', s: 0, err: 'abort' },
    { u: '/api/qa-failed', s: 0, err: 'TypeError: Failed to fetch' },
    { u: '/api/qa-leaving', s: 0, err: 'abort' },
  ])
})
