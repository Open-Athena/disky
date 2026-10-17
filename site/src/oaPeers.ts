import type { Peer } from './hosts'

/** The OA deployments; each site links the others (`Store.peers`), skipping its own host. Identical on every OA branch. */
export const OA_PEERS: Peer[] = [
  { label: 'GCS usage', prod: 'gcs.oa.dev', dev: 'dev.gcs.oa.dev' },
  { label: 'CoreWeave usage', prod: 'cw.oa.dev', dev: 'dev.cw.oa.dev' },
  { label: 'Hugging Face usage', prod: 'hf.oa.dev', dev: 'dev.hf.oa.dev' },
  { label: 'R2 usage', prod: 'r2.oa.dev', dev: 'dev.r2.oa.dev' },
]
