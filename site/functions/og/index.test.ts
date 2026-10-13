import { describe, expect, it } from 'vitest'
import { onRequestGet } from './index'

describe('/og map image shortcut', () => {
  it.each([
    ['https://r2.rbw.sh/og?c=t&card=4', 'https://r2.rbw.sh/og/map.png?c=t&card=4'],
    ['https://r2.rbw.sh/og/?path=ctbk%2Fgbfs&d=261013', 'https://r2.rbw.sh/og/map.png?path=ctbk%2Fgbfs&d=261013'],
    ['https://private.example/og?path=private&sig=test', 'https://private.example/og/map.png?path=private&sig=test'],
    ['https://r2.rbw.sh/og', 'https://r2.rbw.sh/og/map.png'],
  ])('preserves the same-origin view and credentials: %s', (source, target) => {
    const response = onRequestGet({ request: new Request(source) })
    expect([response.status, response.headers.get('location'), response.headers.get('cache-control'), response.body])
      .toEqual([302, target, 'no-store', null])
  })
})
