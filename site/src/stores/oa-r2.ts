import { OA_PEERS } from '../oaPeers'
import type { Store } from '../stores'

const store: Store = {
  key: 'oa-r2',
  label: 'OA R2',
  title: 'Open Athena R2',
  desc: 'Open Athena Cloudflare R2 storage: treemaps, snapshot diffs and last-modified distributions.',
  path: '/',
  scheme: 'r2://',
  base: '/data/r2',
  ogImage: '/og.jpg',
  prices: false,
  defaultColor: 'date',
  reclaimTitleSpace: true,
  emailSignIn: false,
  staging: false,
  owners: false,
  executor: 'plan-sweep',
  buckets: ['marin-na', 'marin-discord', 'oa-cw-s3-usage-index', 'oa-gcs-usage-index'],
  rootLabel: 'all buckets',
  peers: OA_PEERS,
  objectsNote: 'R2 listing dates are LastModified timestamps, not original creation times. Access history is a separate overlay.',
  wall: { restrict: 'This deployment is restricted to Open Athena viewers.', signIn: 'Sign in', how: 'Use your Open Athena email or an approved share link.' },
}

export default store
