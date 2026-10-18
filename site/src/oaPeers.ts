import type { Peer } from './hosts'

/** The OA deployments; each site links the others (`Store.peers`), skipping its own host. Identical on every OA branch.
 * The flags are each one's optional pages (its store's `owners` / `staging`, a `/meta` tree: gcs's redirects to cw's). */
export const OA_PEERS: Peer[] = [
  { label: 'GCS usage', prod: 'gcs.oa.dev', dev: 'dev.gcs.oa.dev', owners: true, staging: true, meta: true },
  { label: 'CoreWeave usage', prod: 'cw.oa.dev', dev: 'dev.cw.oa.dev', staging: true, meta: true },
  { label: 'Hugging Face usage', prod: 'hf.oa.dev', dev: 'dev.hf.oa.dev', meta: true },
  { label: 'R2 usage', prod: 'r2.oa.dev', dev: 'dev.r2.oa.dev', meta: true },
]
