// The `/meta` secondary store (STORES_EXTRA): the data behind hf.oa.dev, scanned by job/profiles/hf-meta.yml.
import type { Store } from '../stores'

const store: Store = {
  key: 'meta', label: 'Meta', title: 'Meta · Open Athena Hugging Face',
  desc: 'The data behind hf.oa.dev: scans, indexes and listings in oa-hf-usage-data (GCS) and oa-hf-usage-index (R2).',
  path: '/meta', scheme: 'r2://', base: '/data/meta', ogImage: '/og.jpg',
  bucketSchemes: { 'oa-hf-usage-data': 'gs://', 'oa-hf-usage-index': 'r2://' },
  prices: false, defaultColor: 'date', reclaimTitleSpace: true,
  staging: false, owners: false, executor: 'plan-sweep',
  buckets: ['oa-hf-usage-data', 'oa-hf-usage-index'], rootLabel: 'this deployment’s storage',
  objectsNote: 'Objects are derived scan outputs and indexes. A meta scan captures their sizes before writing its own outputs.',
  wall: { restrict: 'The meta store is limited to Open Athena staff.', signIn: 'Sign in' },
}

export default store
