import { describe, expect, it } from 'vitest'
import { DEFAULT_PARAMS } from './common'
import { decodeParams, encodeParams, parseParams } from './codec'

describe('digest params in a URL', () => {
  it('round-trips, defaults left out', () => {
    const p = { windowDays: 45, todayAt: [0.6, 0.95] as [number, number], diff: 'mtd' as const, headline: ['window' as const], weeks: 'month' as const }
    expect([encodeParams(DEFAULT_PARAMS), encodeParams(p), decodeParams(encodeParams(p))]).toEqual(['', 'w45,t0.6-0.95,dm,hw,km', p])
    expect(decodeParams('h')).toEqual({ ...DEFAULT_PARAMS, headline: [] })
  })
  it('drops what it cannot read', () => {
    expect(decodeParams('w0,w999,t0.9-0.5,dx,hmm,kq,zz')).toEqual(DEFAULT_PARAMS)
  })
  it('parses strictly for the job', () => {
    const p = { windowDays: 45, todayAt: [0.6, 0.95] as [number, number], diff: 'mtd' as const, headline: ['window' as const], weeks: 'month' as const }
    expect([parseParams(''), parseParams(encodeParams(p)), parseParams('h')]).toEqual([DEFAULT_PARAMS, p, { ...DEFAULT_PARAMS, headline: [] }])
    const errs = ['w0', 'w999', 't0.9-0.5', 'dx', 'hmm', 'kq', 'zz', 'w30,', ',w30', 'w30,w45'].map(s => {
      try {
        parseParams(s)
        return null
      } catch (e) {
        return (e as Error).message
      }
    })
    expect(errs).toEqual([
      'digest params: unreadable field "w0" in "w0"',
      'digest params: unreadable field "w999" in "w999"',
      'digest params: unreadable field "t0.9-0.5" in "t0.9-0.5"',
      'digest params: unreadable field "dx" in "dx"',
      'digest params: unreadable field "hmm" in "hmm"',
      'digest params: unreadable field "kq" in "kq"',
      'digest params: unreadable field "zz" in "zz"',
      'digest params: unreadable field "" in "w30,"',
      'digest params: unreadable field "" in ",w30"',
      'digest params: repeated field "w" in "w30,w45"',
    ])
  })
})
