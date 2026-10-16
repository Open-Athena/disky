import { describe, expect, it } from 'vitest'
import { hubLink } from './stores/hf'

const H = 'https://huggingface.co'
const href = (segs: string[], dir: boolean) => hubLink(segs, dir)?.href

describe('hubLink', () => {
  it('maps the listing layout onto Hub pages', () => {
    expect([
      href(['open-athena'], true),
      href(['open-athena', 'models'], true),
      href(['open-athena', 'buckets'], true),
      href(['open-athena', 'buckets', 'biotasks'], true),
      href(['open-athena', 'buckets', 'biotasks', 'research'], true),
      href(['open-athena', 'buckets', 'biotasks', 'README.md'], false),
      href(['open-athena', 'models', 'm1'], true),
      href(['open-athena', 'models', 'm1', 'ckpt', 'model.safetensors'], false),
      href(['open-athena', 'datasets', 'task-trove', 'data'], true),
      href(['open-athena', 'spaces', 'README', 'README.md'], false),
      href(['open-athena', 'datasets', 'task-trove', '@history', '@unlisted'], false),
      href(['open-athena', 'datasets', 'a b#c', 'x?.json'], false),
      href(['open-athena', 'other'], true),
      href([], true),
    ]).toEqual([
      `${H}/open-athena`,
      `${H}/open-athena/models`,
      `${H}/open-athena/buckets`,
      `${H}/buckets/open-athena/biotasks`,
      `${H}/buckets/open-athena/biotasks/tree/research`,
      `${H}/buckets/open-athena/biotasks/tree/README.md`,
      `${H}/open-athena/m1`,
      `${H}/open-athena/m1/blob/main/ckpt/model.safetensors`,
      `${H}/datasets/open-athena/task-trove/tree/main/data`,
      `${H}/spaces/open-athena/README/blob/main/README.md`,
      `${H}/datasets/open-athena/task-trove/commits/main`,
      `${H}/datasets/open-athena/a%20b%23c/blob/main/x%3F.json`,
      undefined,
      undefined,
    ])
    expect(hubLink(['open-athena'], true)?.label).toBe('on Hugging Face')
  })
})
