# HF cs3: storage proportional to changes

Approved 2026-10-21. Preserve all historical map, diff and age-chart answers while removing full per-scan copies from permanent storage. Raw build inputs and operational reports use bounded retention; a small scan/run ledger remains per scan.

## Implementation

1. Merge the local `cloud` branch, retaining HF's storage-class names and deployment configuration. Validate the merge before deployment.
2. Make interval-store locations explicit per site store, with independent prefixes and current-generation pointers. Enable an interval store for `/meta`, build its complete history on Batch, verify parity, then retire redundant meta path/bysize copies while keeping two recent inputs.
3. Reuse the interval-store engine for age histograms: encode each `(tier, original path, binstart)` cell as a stable synthetic path; bytes and object count are its values. Build historical versions from existing pyramids, verify every cell before publication, and append only changed versions thereafter. The age API reads these cells through the existing range reader; no per-scan age file is deleted until a verified age-store manifest holds its scan. Namespace age history independently for the primary and meta stores.
4. Bound meta raw listings and operational reports. Retain raw inputs for 30 days, full HTML reports for 30 days, work/cache for 14 days and scratch for seven days. Keep the small DAG/trace and scan/run records as the explicit operational ledger. Preserve report links for retained reports and make expiration explicit for older reports.
5. Build the job image, deploy dev/prod, build and verify the historical stores on Batch, enable retirement, and run manual HF and meta scans. Check historical map/diff/age parity, warm-up, serving probe and cleanup logs. Record live cutovers and measurements here and in `hf-deployment.md`.

## Acceptance

- Unchanged histogram cells and path rows produce no new versions; changed, removed and zero-byte cells reconstruct exactly at every scan, including after compaction.
- The primary, meta and both age stores have independent pointers, data and cache namespaces. A secondary-store request never reads primary history.
- Publication is data before manifests/pointers. A failed build or append leaves the previously published history readable; an uncovered scan keeps its source artifacts.
- Retirement checks both path/static coverage and age coverage before deleting the relevant files, on every copy and in D1. Recent inputs remain available for catch-up.
- Tests compare complete structured answers and artifact inventories. Live verification runs on Batch, not the laptop. No whole-org listing runs on the laptop.
- No secret values or account identifiers are added to tracked files.

## Progress

- Shared `cloud` merged through `76acbcbb` as `c4779025`; 163 targeted merge checks passed.
- Shared implementation in `35954b69`: independent interval prefixes and cache namespaces, verified age-cell history and gated retirement, independent static pointers, successor-aware retirement/cleanup. Optional features stay off outside configured deployments. Site: 2,596 tests passed, both typechecks and build passed; targeted cloud history, compaction, retirement and profile checks passed.
- HF's two profiles configure independent primary-age, meta-path and meta-age roots, keep two recent full inputs and collect superseded generations. Static lineages publish separate pointers while retaining their original roots as fixed rollback anchors. Both site's environments follow those pointers.
- Retention recipe: primary/meta raw 30 days, full Nextflow report/timeline HTML 30 days, work/cache 14 days, scratch seven days. DAG/trace and run/scan records remain as the small operational ledger. The site's custom run DAG/timeline reads the ledger; it has no direct full-report links to invalidate. Five HF stack/profile mock checks passed. Infrastructure apply pending.
- HF bucket listing improvement is separately shareable as `390852b5`: request 5,000 entries per bucket-tree page (the official OpenAPI maximum) and preserve server pagination. 432 engine tests passed and mypy is clean over 56 source files. This does not change repo commit-date semantics.
- Cloud and GCS transcripts checked for overlapping work; no parallel implementation of age history or secondary interval namespaces found. Shared ownership handoff left under the cloud worktree's `specs/cs3-history-handoff.md`. Research and a local, unposted `thrds` draft about large HF bucket inventories are in progress alongside rollout.
- Pending: candidate image, Batch history bootstrap and parity, pointer cutover, dev/prod rollout, scoped lifecycle apply and manual scans. Browser connector unavailable in this Codex thread; CIC has not been performed.
