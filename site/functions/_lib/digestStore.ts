/**
 * Where a digest's inputs and converge state live, for a Worker's converge (specs/slack-in-workers.md): the snapshots
 * root's scan metas (read) and the per-month thread state JSON beside them (read, then written by compare-and-swap).
 * One interface over R2 (a bucket binding) and GCS (the JSON API under the site's service-account key), so a
 * deployment's converge reads and writes exactly what the job's `blobfs` does, at the same keys.
 *
 * A write names the version it read (R2's etag, GCS's generation; null: the object must not exist yet) and is refused,
 * not applied, when another writer moved it meanwhile: the caller re-reads and re-plans.
 */
import type { R2Bucket } from '@cloudflare/workers-types'
import { gcpToken } from './gcp.js'
import { monthIds, monthRows, scanIds, type ScanMeta } from '../../src/digest/load.js'
import type { QuotaRows } from '../../src/digest/quota.js'

export interface Versioned<T> { value: T; version: string }

export interface SnapshotStore {
  /** The store's URL scheme and bucket (`r2://oa-r2-usage-index`), for messages. */
  readonly where: string
  /** Every key under ``prefix``. */
  list(prefix: string): Promise<string[]>
  /** ``key``'s text; null when there's none. */
  getText(key: string): Promise<string | null>
  /** ``key``'s JSON with its version; null when there's none. */
  getJson<T>(key: string): Promise<Versioned<T> | null>
  /** ``value`` at ``key`` if it's still at ``ifVersion`` (null: absent); its new version, or null when refused. */
  putJson(key: string, value: unknown, ifVersion: string | null): Promise<string | null>
}

const LIST_PAGE = 1000

/** An R2 bucket through its binding. */
export function r2Store(bucket: R2Bucket, name: string): SnapshotStore {
  return {
    where: `r2://${name}`,
    async list(prefix) {
      const out: string[] = []
      let cursor: string | undefined
      do {
        const page = await bucket.list({ prefix, cursor, limit: LIST_PAGE })
        out.push(...page.objects.map(o => o.key))
        cursor = page.truncated ? page.cursor : undefined
      } while (cursor)
      return out
    },
    async getText(key) {
      const o = await bucket.get(key)
      return o ? o.text() : null
    },
    async getJson<T>(key: string) {
      const o = await bucket.get(key)
      return o ? { value: (await o.json()) as T, version: o.etag } : null
    },
    async putJson(key, value, ifVersion) {
      const onlyIf = ifVersion === null ? new Headers({ 'if-none-match': '*' }) : { etagMatches: ifVersion }
      const put = await bucket.put(key, JSON.stringify(value, null, 2), { onlyIf, httpMetadata: { contentType: 'application/json' } })
      return put ? put.etag : null
    },
  }
}

const GCS = 'https://storage.googleapis.com'

/** A GCS bucket through the JSON API, authenticated as the site's service account (``saKey``: `GCP_SA_KEY`). */
export function gcsStore(saKey: string, name: string, fetcher: typeof fetch = fetch): SnapshotStore {
  const auth = async () => ({ authorization: `Bearer ${await gcpToken(saKey)}` })
  const obj = (key: string) => `${GCS}/storage/v1/b/${encodeURIComponent(name)}/o/${encodeURIComponent(key)}`
  const get = async (key: string): Promise<Response | null> => {
    const r = await fetcher(`${obj(key)}?alt=media`, { headers: await auth() })
    if (r.status === 404) return null
    if (!r.ok) throw new Error(`gs://${name}/${key}: ${r.status} ${await r.text()}`)
    return r
  }
  return {
    where: `gs://${name}`,
    async list(prefix) {
      const out: string[] = []
      let pageToken: string | undefined
      do {
        const q = new URLSearchParams({ prefix, fields: 'items(name),nextPageToken', maxResults: String(LIST_PAGE) })
        if (pageToken) q.set('pageToken', pageToken)
        const r = await fetcher(`${GCS}/storage/v1/b/${encodeURIComponent(name)}/o?${q}`, { headers: await auth() })
        if (!r.ok) throw new Error(`gs://${name}/${prefix}: list ${r.status} ${await r.text()}`)
        const page = (await r.json()) as { items?: { name: string }[]; nextPageToken?: string }
        out.push(...(page.items ?? []).map(i => i.name))
        pageToken = page.nextPageToken
      } while (pageToken)
      return out
    },
    async getText(key) {
      const r = await get(key)
      return r ? r.text() : null
    },
    async getJson<T>(key: string) {
      const r = await get(key)
      if (!r) return null
      const generation = r.headers.get('x-goog-generation')
      if (!generation) throw new Error(`gs://${name}/${key}: no generation in the answer`)
      return { value: (await r.json()) as T, version: generation }
    },
    async putJson(key, value, ifVersion) {
      const q = new URLSearchParams({ uploadType: 'media', name: key, ifGenerationMatch: ifVersion ?? '0' })
      const r = await fetcher(`${GCS}/upload/storage/v1/b/${encodeURIComponent(name)}/o?${q}`, {
        method: 'POST', headers: { ...(await auth()), 'content-type': 'application/json' }, body: JSON.stringify(value, null, 2),
      })
      if (r.status === 412) return null
      if (!r.ok) throw new Error(`gs://${name}/${key}: write ${r.status} ${await r.text()}`)
      return ((await r.json()) as { generation: string }).generation
    },
  }
}

/** A digest root URL (`r2://<bucket>/<prefix>`, `gs://<bucket>/<prefix>`) split. */
export function parseRoot(root: string): { scheme: 'r2' | 'gs'; bucket: string; prefix: string } {
  const m = /^(r2|gs):\/\/([a-z0-9][a-z0-9._-]*)\/(.+?)\/?$/.exec(root)
  if (!m) throw new Error(`digest root ${JSON.stringify(root)}: not an r2:// or gs:// URL with a path`)
  return { scheme: m[1] as 'r2' | 'gs', bucket: m[2], prefix: m[3] }
}

export interface StoreEnv { INDEX_R2?: R2Bucket; STORE_BUCKET?: string; GCP_SA_KEY?: string }

/** ``root``'s store: an `r2://` root on the bucket the site binds (`INDEX_R2`, named by `STORE_BUCKET`), or a `gs://`
 *  root through `GCP_SA_KEY`. Anything else is a config error, said as one. */
export function storeFor(env: StoreEnv, root: string): SnapshotStore {
  const { scheme, bucket } = parseRoot(root)
  if (scheme === 'r2') {
    if (!env.INDEX_R2 || env.STORE_BUCKET !== bucket) throw new Error(`digest root ${root}: the site binds no R2 bucket ${bucket} (INDEX_R2 is ${env.STORE_BUCKET ?? 'unset'})`)
    return r2Store(env.INDEX_R2, bucket)
  }
  if (!env.GCP_SA_KEY) throw new Error(`digest root ${root}: no GCP_SA_KEY to read GCS with`)
  return gcsStore(env.GCP_SA_KEY, bucket)
}

/** The number of meta reads in flight at once (Workers allow 6 open connections an invocation). */
const READS = 6

/** ``month``'s rows from ``root``'s scans (`load_month` → `rows_json`), as of ``now``; null when it has no scans. */
export async function loadMonth(store: SnapshotStore, root: string, month: string, primary: string, unit: number,
  { asOf = null, now = new Date() }: { asOf?: string | null; now?: Date } = {}): Promise<QuotaRows | null> {
  const { prefix } = parseRoot(root)
  const ids = monthIds(scanIds((await store.list(`${prefix}/`)).map(k => k.slice(prefix.length))), month, asOf)
  if (!ids) return null
  const want = [...ids.pre, ...ids.lead, ...ids.inMonth]
  const metas: Record<string, ScanMeta> = {}
  for (let i = 0; i < want.length; i += READS) {
    await Promise.all(want.slice(i, i + READS).map(async s => {
      const t = await store.getText(`${prefix}/${s}/meta.json`)
      if (t === null) throw new Error(`${store.where}/${prefix}/${s}/meta.json: listed, then gone`)
      metas[s] = JSON.parse(t) as ScanMeta
    }))
  }
  return monthRows(ids, metas, primary, unit, now)
}
