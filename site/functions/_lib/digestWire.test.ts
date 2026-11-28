/** The digest's Slack wire goldens: `cloud/tests/fixtures/digest/wire/payloads.json` holds what the job's thrds client
 * sends to post and to edit each digest body (pinned by `cloud/tests/test_digest_wire_parity.py`). */
import { describe, expect, it } from 'vitest'
import { bustUrl, editPayload, postPayload, toSlack } from './digestWire'

interface Fs { readFile(p: string, enc: 'utf8'): Promise<string> }
const fsp = () => import(/* @vite-ignore */ 'node:fs/promises' as string) as Promise<Fs>
const FILE = new URL('../../../cloud/tests/fixtures/digest/wire/payloads.json', import.meta.url).pathname

interface Case { body: string; post: Record<string, unknown>; edit: Record<string, unknown> }
interface Fixture { bust: string; cases: Record<string, Case> }

describe('digest wire goldens', () => {
  it('posts and edits every body as the job does', async () => {
    const f: Fixture = JSON.parse(await (await fsp()).readFile(FILE, 'utf8'))
    expect(Object.keys(f.cases).length).toBe(63)
    for (const [name, c] of Object.entries(f.cases)) {
      const post = postPayload('C0TEST', c.body, { threadTs: '1.000000', sender: { username: 'wk of 11/2', icon_url: 'https://i.example/mark.png' }, bust: f.bust })
      expect([name, post]).toEqual([name, c.post])
      expect([name, editPayload('C0TEST', '1.000002', c.body, { bust: f.bust })]).toEqual([name, c.edit])
    }
  })
  it('leaves arithmetic and identifiers alone, converts emphasis beside punctuation', () => {
    expect(toSlack('2*4*8 a_b *x*, **y** [l](u)')).toBe('2*4*8 a_b _x_, *y* <u|l>')
  })
  it('re-encodes a busted URL\'s query as Python does', () => {
    expect(bustUrl('https://h/p.png?v=1&thrds_bust=9&a=b+c#f', '7')).toBe('https://h/p.png?v=1&a=b+c&thrds_bust=7#f')
  })
})
