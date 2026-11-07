import { hashKey } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'
import { budgetCutRefetch, noteRetryAfter, REASK_FALLBACK_MS, REASK_MAX, REASK_TOTAL_MS, RETRY_AFTER_MAX_MS, RETRY_AFTER_MIN_MS } from './budgetCut'

let keys = 0
/** A query (react-query's `Query`: one object across its fetches) of a fresh key. */
const query = () => {
  const key = ['subtree', keys++]
  return { key, queryHash: hashKey(key), state: { data: undefined as { budgetCut?: boolean } | undefined, dataUpdateCount: 0 } }
}
type Q = ReturnType<typeof query>
const CUT = { budgetCut: true as const }
/** `q` after its `fetches`-th fetch answered `data` with `Retry-After: retryAfter` (seconds; none: absent). */
const fetched = (q: Q, data: { budgetCut?: boolean }, fetches: number, retryAfter?: string): Q => {
  noteRetryAfter(q.key, new Headers(retryAfter == null ? {} : { 'retry-after': retryAfter }))
  q.state = { data, dataUpdateCount: fetches }
  return q
}

describe('budgetCutRefetch', () => {
  it('a whole answer, or none, is never re-asked', () => {
    expect([budgetCutRefetch(query()), budgetCutRefetch(fetched(query(), {}, 1)), budgetCutRefetch(fetched(query(), {}, 1, '7'))]).toEqual([false, false, false])
  })

  it('honours a cut answer\'s `Retry-After`, held to [`RETRY_AFTER_MIN_MS`, `RETRY_AFTER_MAX_MS`]; unreadable is absent', () => {
    expect(['7', '3', '0', '60', 'soon'].map(s => budgetCutRefetch(fetched(query(), CUT, 1, s), 0))).toEqual([
      7000, 3000, RETRY_AFTER_MIN_MS, RETRY_AFTER_MAX_MS, REASK_FALLBACK_MS,
    ])
  })

  it('the latest response\'s `Retry-After` counts, though the answer is the same object (structural sharing)', () => {
    const q = query()
    expect([budgetCutRefetch(fetched(q, CUT, 1, '7'), 0), budgetCutRefetch(fetched(q, CUT, 2, '3'), 7000), budgetCutRefetch(fetched(q, CUT, 3), 10_000)]).toEqual([7000, 3000, REASK_FALLBACK_MS])
  })

  it('without `Retry-After`: every `REASK_FALLBACK_MS`, as many as fit in `REASK_TOTAL_MS` from the first cut answer', () => {
    const q = query()
    const waits: (number | false)[] = []
    let t = 0
    for (let fetches = 1; fetches <= REASK_MAX + 1; fetches++) {
      const w = budgetCutRefetch(fetched(q, CUT, fetches), t)
      waits.push(w)
      if (w === false) break
      t += w
    }
    expect([waits, REASK_TOTAL_MS / REASK_FALLBACK_MS]).toEqual([[10_000, 10_000, 10_000, 10_000, 10_000, 10_000, false], 6])
  })

  it('at most `REASK_MAX` re-asks, and none past `REASK_TOTAL_MS`, whatever `Retry-After` says', () => {
    const fast = query()
    const n: (number | false)[] = []
    for (let fetches = 1, t = 0; fetches <= REASK_MAX + 2; fetches++, t += 1000) n.push(budgetCutRefetch(fetched(fast, CUT, fetches, '1'), t))
    const slow = query()
    const s = [0, 20_000, 40_000, 41_000].map((t, i) => budgetCutRefetch(fetched(slow, CUT, i + 1, '20'), t))
    expect([n, s]).toEqual([[...Array(REASK_MAX).fill(1000), false, false], [20_000, 20_000, 20_000, false]])
  })

  it('a whole answer ends the chain: a later cut answer of the same query starts a new one', () => {
    const q = query()
    const got = [budgetCutRefetch(fetched(q, CUT, 1), 0), budgetCutRefetch(fetched(q, {}, 2), 50_000), budgetCutRefetch(fetched(q, CUT, 3), 55_000)]
    expect(got).toEqual([REASK_FALLBACK_MS, false, REASK_FALLBACK_MS])
  })
})
