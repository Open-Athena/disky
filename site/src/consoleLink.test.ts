import { describe, expect, it } from 'vitest'
import { consoleLink } from './stores/gcs'

const C = 'https://console.cloud.google.com/storage/browser'
const label = 'in the Cloud Console'

describe('consoleLink', () => {
  it('has no page for the store root', () => {
    expect(consoleLink([], true)).toEqual(undefined)
  })
  it('links a bucket', () => {
    expect(consoleLink(['marin-us-central2'], true)).toEqual({ href: `${C}/marin-us-central2`, label })
  })
  it('links a folder by its prefix, with a trailing slash', () => {
    expect(consoleLink(['marin-us-central2', 'checkpoints', 'run 1'], true)).toEqual({ href: `${C}/marin-us-central2/checkpoints/run%201/`, label })
  })
  it("links an object's details page", () => {
    expect(consoleLink(['marin-us-east1', 'a', 'b#c.json'], false)).toEqual({ href: `${C}/_details/marin-us-east1/a/b%23c.json`, label })
  })
})
