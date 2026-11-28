/** The month loader's goldens: `cloud/tests/fixtures/digest/load/months.json` holds a snapshots root's listing, its
 * metas and `scan_runs` rows, and each case's rows as the job reads them (pinned by
 * `cloud/tests/test_digest_load_parity.py`). */
import { describe, expect, it } from 'vitest'
import { monthIds, monthRows, type RunTrigger, scanIds, type ScanMeta, scanTriggers } from './load'
import type { QuotaRows } from './quota'

interface Fs { readFile(p: string, enc: 'utf8'): Promise<string> }
const fsp = () => import(/* @vite-ignore */ 'node:fs/promises' as string) as Promise<Fs>
const FILE = new URL('../../../cloud/tests/fixtures/digest/load/months.json', import.meta.url).pathname

interface Case { name: string; month: string; primary: string; unit: number; as_of: string | null; rows: QuotaRows | null }
interface Fixture {
  now: string; keys: string[]; metas: Record<string, ScanMeta>; runs: RunTrigger[]; triggers: Record<string, string>
  scans: string[]; cases: Case[]
}

const fixture = async (): Promise<Fixture> => JSON.parse(await (await fsp()).readFile(FILE, 'utf8'))

describe('digest month loader goldens', () => {
  it('lists the published scans as the job does', async () => {
    const f = await fixture()
    expect(scanIds(f.keys)).toEqual(f.scans)
  })
  it('reads each scan\'s trigger from its first run', async () => {
    const f = await fixture()
    expect(scanTriggers(f.runs)).toEqual(f.triggers)
  })
  it('loads each case\'s month exactly', async () => {
    const f = await fixture()
    expect(f.cases.map(c => c.name)).toEqual([
      'october-first', 'november-tb', 'november-tib', 'november-as-of-10th', 'november-side', 'december-open', 'january-none',
    ])
    const scans = scanIds(f.keys)
    for (const c of f.cases) {
      const ids = monthIds(scans, c.month, c.as_of)
      const got = ids && monthRows(ids, f.metas, c.primary, c.unit, new Date(f.now))
      expect([c.name, got]).toEqual([c.name, c.rows])
    }
  })
})
