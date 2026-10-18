// hf.oa.dev's store: the Hugging Face org `open-athena` (its Storage Buckets and model, dataset and space repos) as
// one bucket, listed daily by the scan's `list` step (`disk-tree bulk-list hf://open-athena`).
import { OA_PEERS } from '../oaPeers'
import type { ExternalLink, Store } from '../stores'

/** The org's storage allowances, as declared values: the Hub has no API for them. Source: the org's billing page
 *  (huggingface.co/organizations/open-athena/settings/billing), read 2026-10-14 — public storage 15.7 TB used of
 *  253 TB (incl. a 245 TB contract add-on), private 0.02 TB of 5.1 TB (incl. a 5 TB add-on), egress 5.12 TB of
 *  30 TB this month. Update them here when the contract changes. Bytes are decimal (TB = 10¹²), as the page shows. */
export const HF_ALLOWANCES = {
  publicStorage: 253e12,
  privateStorage: 5.1e12,
  egressPerMonth: 30e12,
} as const

const HUB = 'https://huggingface.co'
const REPO_KINDS = new Set(['models', 'datasets', 'spaces'])
const enc = (segs: string[]) => segs.map(encodeURIComponent).join('/')

/** A path's page on the Hub, by the listing's layout (`find/bulk_hf.py`): `<org>/buckets/<b>/…` →
 *  `/buckets/<org>/<b>/tree/…` (files and folders alike); `<org>/{models,datasets,spaces}/<repo>/…` → the repo's
 *  `tree/main/…` (folder) or `blob/main/…` (file), a model repo without the kind prefix; a repo's `@history` (the
 *  versions its tree no longer references) → its commits; the org and a kind's folder → the org's listing tab. */
export function hubLink(segs: string[], dir: boolean): ExternalLink | undefined {
  const [org, kind, name, ...rest] = segs
  if (!org) return undefined
  const label = 'on Hugging Face'
  if (!kind) return { href: `${HUB}/${enc([org])}`, label }
  if (kind !== 'buckets' && !REPO_KINDS.has(kind)) return undefined
  if (!name) return { href: `${HUB}/${enc([org, kind])}`, label }
  if (kind === 'buckets') return { href: `${HUB}/${enc(['buckets', org, name])}${rest.length ? `/tree/${enc(rest)}` : ''}`, label }
  const repo = `${HUB}/${enc(kind === 'models' ? [org, name] : [kind, org, name])}`
  if (!rest.length) return { href: repo, label }
  if (rest[0] === '@history') return { href: `${repo}/commits/main`, label }
  return { href: `${repo}/${dir ? 'tree' : 'blob'}/main/${enc(rest)}`, label }
}

const TB = (b: number) => `${+(b / 1e12).toFixed(1)} TB`

const store: Store = {
  key: 'hf',
  label: 'Hugging Face',
  title: 'Open Athena Hugging Face usage',
  desc: 'Storage usage of the open-athena Hugging Face org — its Storage Buckets and model, dataset and space repos — treemap and diffs over time.',
  about: (
    <>
      <p>
        Every file in the <code>open-athena</code> org on Hugging Face, listed daily and summed by directory:
        {' '}<code>buckets/&lt;bucket&gt;/…</code> for Storage Buckets, <code>models/</code>, <code>datasets/</code> and <code>spaces/</code> for repos.
        A repo’s sizes add up to what Hugging Face bills it (<code>usedStorage</code>): each stored LFS / Xet object once,
        with older versions the current tree no longer references in one <code>@history/@unlisted</code> row per repo.
        Plain git files (READMEs, small JSON) are listed at their size but aren’t billed.
      </p>
      <p>
        The org’s allowances (billing page, as of 2026-10-14): public storage {TB(HF_ALLOWANCES.publicStorage)},
        private storage {TB(HF_ALLOWANCES.privateStorage)}, egress {TB(HF_ALLOWANCES.egressPerMonth)} a month.
      </p>
    </>
  ),
  path: '/',
  // the org is the store's one bucket, mounted at `/` (`/models/…`, no `/open-athena` segment or crumb); `STORE_ROOT` in wrangler.toml
  root: ['open-athena'],
  scheme: 'hf://',
  base: '/data/hf',
  ogImage: '/og.jpg',
  prices: false,
  staging: false,
  owners: false,
  executor: 'plan-sweep',
  buckets: ['open-athena'],
  rootLabel: 'the whole org',
  externalLink: hubLink,
  peers: OA_PEERS,
  allowances: {
    rows: [
      { label: 'Public storage', bytes: HF_ALLOWANCES.publicStorage, name: '253 TB', classes: [1] },
      { label: 'Private storage', bytes: HF_ALLOWANCES.privateStorage, name: '5.1 TB', classes: [5] },
    ],
    source: { label: 'billing page', href: `${HUB}/organizations/open-athena/settings/billing`, asof: '2026-10-14' },
    note: <>Plain git files (READMEs, small JSON) count toward neither. Egress (downloads) has a separate {TB(HF_ALLOWANCES.egressPerMonth)} a month, which listings can’t measure.</>,
  },
  objectsNote: 'A repo file’s time is its last commit; an old version’s is when it was pushed; a Storage Bucket file’s is its upload.',
  wall: {
    restrict: 'Access is limited to Open Athena staff.',
    signIn: 'Sign in with Open Athena',
    how: 'Use Google with your Open Athena account, or have a one-time code emailed to it (no Google account needed). Invited guests can also use a personal share link.',
  },
}

export default store
