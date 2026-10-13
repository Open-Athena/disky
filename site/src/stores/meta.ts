import type { Store } from '../stores'

const store: Store = {
  key: 'meta', label: 'Meta', title: 'Meta · Open Athena R2',
  desc: 'This deployment’s own scan archive and serving indexes in oa-r2-usage-data (GCS) and oa-r2-usage-index (R2).',
  path: '/meta', scheme: 'r2://', base: '/data/meta', ogImage: '/og.jpg',
  bucketSchemes: { 'oa-r2-usage-data': 'gs://', 'oa-r2-usage-index': 'r2://' },
  prices: false, defaultColor: 'date', reclaimTitleSpace: true,
  emailSignIn: false, staging: false, owners: false, executor: 'plan-sweep',
  buckets: ['oa-r2-usage-data', 'oa-r2-usage-index'], rootLabel: 'this deployment’s storage',
  objectsNote: 'Objects are derived scan outputs and indexes. A meta scan captures their sizes before writing its own outputs.',
  wall: { restrict: 'This store is restricted to Open Athena R2 administrators.', signIn: 'Sign in' },
}

export default store
