// hf.oa.dev's store: the Hugging Face org `open-athena` (its Storage Buckets and model, dataset and space repos) as
// one bucket, listed daily by job/hf-run.sh (`disk-tree bulk-list hf://open-athena`).
import type { Store } from '../stores'

/** The org's storage allowances, as declared values: the Hub has no API for them. Source: the org's billing page
 *  (huggingface.co/organizations/open-athena/settings/billing), read 2026-10-14 — public storage 15.7 TB used of
 *  253 TB (incl. a 245 TB contract add-on), private 0.02 TB of 5.1 TB (incl. a 5 TB add-on), egress 5.12 TB of
 *  30 TB this month. Update them here when the contract changes. Bytes are decimal (TB = 10¹²), as the page shows. */
export const HF_ALLOWANCES = {
  publicStorage: 253e12,
  privateStorage: 5.1e12,
  egressPerMonth: 30e12,
} as const

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
  scheme: 'hf://',
  base: '/data/hf',
  ogImage: '/og.jpg',
  prices: false,
  staging: false,
  owners: false,
  executor: 'plan-sweep',
  buckets: ['open-athena'],
  rootLabel: 'the whole org',
  objectsNote: 'A repo file’s time is its last commit; an old version’s is when it was pushed; a Storage Bucket file’s is its upload.',
  wall: {
    restrict: 'Access is limited to Open Athena staff.',
    signIn: 'Sign in with Open Athena',
    how: 'Use Google with your Open Athena account, or have a one-time code emailed to it (no Google account needed). Invited guests can also use a personal share link.',
  },
}

export default store
