# Spec index and audit checkpoint

Audited 2026-10-11 in the GCS worktree. The original inventory had 185 Markdown specs (73 outside `done/`, 112 archived). Adding this index and [GCS operations][operations] makes 187 (75 outside `done/`, 112 archived). Being outside `done/` does **not** mean an item is unfinished.

## How to read status

[GCS operations][operations] is the current deployment handoff. Its release state takes precedence over dated rollout proposals; [the architecture index][architecture] separates accepted production contracts from experiments. A current-status correction at a spec's top supersedes its old opening checkpoint, not its historical measurements. Shared code presence does not prove migration/application on CW, local or other deployments.

The audit inventories every Markdown spec and checks local Markdown file targets, including archives. It screens every active opening status and reconciles actionable GCS claims against source, the correct GCS Claude history, live IaC/deployment results and browser checks. It does not rerun every historical benchmark, verify every external URL/anchor, or assert fresh live state for other deployments. Archived measurements and rejected trials are retained rather than rewritten into new acceptance claims.

## Reconciled operational gates

- Done: R2 small-artifact serving in production; required R2 copy/verification before D1 visibility; Pulumi Output import-ID fix; dedicated read-only snapshot grant/secret/scheduler wiring; static/interval/D1 concurrency and current GCS rollouts.
- Awaiting measurement: the built CPU drill fix's projected speedup. Prefetch alone did not improve the measured drill. The approved manual scan completed; the scheduled scan runs at 07:00 UTC.
- Oct 12 follow-through: five reviewed obsolete derived generations were removed after fresh pointer/input-reference checks, with seven-day recovery. Source-object purging, compaction, fleet expansion, billing-export administration and retirement of the old account-wide R2 token remain outside that approval. Manual scan `2026-10-12T1042` completed R2/interval/D1 publication, but original search catch-up failed in the daily long drill. Instrumented recovery confirmed host OOM; streaming insertion, reserved DuckDB headroom and avoiding bulk Python key caches passed 104 full drill tests and 133 targeted regressions. The real-data retry and catch-up controller succeeded with zero OOM events; both scans have complete serving coverage. The final job fix is promoted to `:latest`. Production Pages `f4ad1682` also fixes false light-term refusals from accumulated boundary groups (142 reader tests); exact API queries and Chrome search render successfully. The projected drill speedup remains unvalidated.
- Kept distinct: normal parallel guided deletion versus the built adaptive option. No real-delete throughput benchmark or new source deletion was launched.

## Document routing

### Current GCS contracts and rollout handoffs

Use the current-status corrections and operational runbook; older proposal sections remain provenance.

[gcs-operations.md][g0-0], [architecture/README.md][g0-1], [architecture/serverless.md][g0-2], [architecture/static-name-search.md][g0-3], [path-store.md][g0-4], [interval-store.md][g0-5], [static-append.md][g0-6], [interval-store-append-gcs.md][g0-7], [anchored-search.md][g0-8], [search-extensions.md][g0-9], [scan-runs-ui.md][g0-10], [health-page.md][g0-11], [scan-ids-not-dates.md][g0-12], [share-link-hardening.md][g0-13], [staged-delete.md][g0-14], [sweep-executor.md][g0-15], [actions-ledger.md][g0-16], [file-assign.md][g0-17], [treemap-scaling.md][g0-18], [cost-labels.md][g0-19], [cost-labels-gcs.md][g0-20], [deployment-divergence.md][g0-21].

### Retired experiments and superseded handoffs

Historical evidence only. Do not restart the query box or replay completed drill integration.

[architecture/clickhouse.md][g1-0], [architecture/mega-index.md][g1-1], [architecture/short-query-index.md][g1-2], [hot-preview-release.md][g1-3], [static-drill-handoff.md][g1-4].

### Shared or partial implementation tracks

Keep each document's as-built versus remaining sections separate. Code presence is not rollout acceptance for every deployment.

[bysize-path-total.md][g2-0], [listing-slim.md][g2-1], [multi-store.md][g2-2], [oa-decoupling.md][g2-3], [iac-templates.md][g2-4], [render-bench.md][g2-5], [static-hex-runs.md][g2-6], [branch-layout.md][g2-7], [union-of-roots.md][g2-8], [sweep-plan-union.md][g2-9], [digest-unification.md][g2-10].

### Proposals, research and explicitly gated expansion

No new implementation/deployment/data-write authority is inferred from this audit. Parked items stay parked.

[assignment-provenance.md][g3-0], [footers-store.md][g3-1], [latency-observability.md][g3-2], [federated-scans.md][g3-3], [pyrmts-adoption.md][g3-4], [obs-axis-indexing.md][g3-5], [org-axis.md][g3-6], [fleet-long-tail.md][g3-7], [private-discord-archive-corpus.md][g3-8], [assign-by-search.md][g3-9], [digest-doc-push.md][g3-10], [r2-sql-eval.md][g3-11], [reencode-opportunity.md][g3-12], [staging-envs.md][g3-13], [viz-prototypes.md][g3-14].

### Dated audits, design baselines and specialist evidence

Read their dates and named branches. They are not current GCS rollout commands; open follow-ups need fresh evidence/authority.

[access-logs-and-cost.md][g4-0], [atime-accumulators.md][g4-1], [cf-iac.md][g4-2], [cloud-placement.md][g4-3], [data-locality-and-retention.md][g4-4], [dir-agg-cache.md][g4-5], [favicon-marks.md][g4-6], [index-landscape.md][g4-7], [oa-store-inventory.md][g4-8], [path-store-search.md][g4-9], [reactive-ingest.md][g4-10], [serving-options.md][g4-11], [slack-notifier.md][g4-12], [storage-cost-attribution.md][g4-13], [sweep-coowned-residue.md][g4-14], [view-serving.md][g4-15], [wandb-attr-prod.md][g4-16], [webdata-memory.md][g4-17], [architecture/alternatives.md][g4-18], [ch-store-retirement.md][g4-19], [base-sync-2026-10-10.md][g4-20].

## Repeatable audit

Run `scripts/spec-audit.py` for the inventory/link summary, or `scripts/spec-audit.py -j` for every path, opening status and broken local target. The parser ignores fenced and inline code examples, including multiline code spans; a precise fixture test pins those distinctions. The Oct 11 audit finishes with zero broken local file targets. Retired-code and cross-project references now use pinned source revisions instead of worktree-relative paths.

[operations]: gcs-operations.md
[architecture]: architecture/README.md
[g0-0]: gcs-operations.md
[g0-1]: architecture/README.md
[g0-2]: architecture/serverless.md
[g0-3]: architecture/static-name-search.md
[g0-4]: path-store.md
[g0-5]: interval-store.md
[g0-6]: static-append.md
[g0-7]: interval-store-append-gcs.md
[g0-8]: anchored-search.md
[g0-9]: search-extensions.md
[g0-10]: scan-runs-ui.md
[g0-11]: health-page.md
[g0-12]: scan-ids-not-dates.md
[g0-13]: share-link-hardening.md
[g0-14]: staged-delete.md
[g0-15]: sweep-executor.md
[g0-16]: actions-ledger.md
[g0-17]: file-assign.md
[g0-18]: treemap-scaling.md
[g0-19]: cost-labels.md
[g0-20]: cost-labels-gcs.md
[g0-21]: deployment-divergence.md
[g1-0]: architecture/clickhouse.md
[g1-1]: architecture/mega-index.md
[g1-2]: architecture/short-query-index.md
[g1-3]: hot-preview-release.md
[g1-4]: static-drill-handoff.md
[g2-0]: bysize-path-total.md
[g2-1]: listing-slim.md
[g2-2]: multi-store.md
[g2-3]: oa-decoupling.md
[g2-4]: iac-templates.md
[g2-5]: render-bench.md
[g2-6]: static-hex-runs.md
[g2-7]: branch-layout.md
[g2-8]: union-of-roots.md
[g2-9]: sweep-plan-union.md
[g2-10]: digest-unification.md
[g3-0]: assignment-provenance.md
[g3-1]: footers-store.md
[g3-2]: latency-observability.md
[g3-3]: federated-scans.md
[g3-4]: pyrmts-adoption.md
[g3-5]: obs-axis-indexing.md
[g3-6]: org-axis.md
[g3-7]: fleet-long-tail.md
[g3-8]: private-discord-archive-corpus.md
[g3-9]: assign-by-search.md
[g3-10]: digest-doc-push.md
[g3-11]: r2-sql-eval.md
[g3-12]: reencode-opportunity.md
[g3-13]: staging-envs.md
[g3-14]: viz-prototypes.md
[g4-0]: access-logs-and-cost.md
[g4-1]: atime-accumulators.md
[g4-2]: cf-iac.md
[g4-3]: cloud-placement.md
[g4-4]: data-locality-and-retention.md
[g4-5]: dir-agg-cache.md
[g4-6]: favicon-marks.md
[g4-7]: index-landscape.md
[g4-8]: oa-store-inventory.md
[g4-9]: path-store-search.md
[g4-10]: reactive-ingest.md
[g4-11]: serving-options.md
[g4-12]: slack-notifier.md
[g4-13]: storage-cost-attribution.md
[g4-14]: sweep-coowned-residue.md
[g4-15]: view-serving.md
[g4-16]: wandb-attr-prod.md
[g4-17]: webdata-memory.md
[g4-18]: architecture/alternatives.md
[g4-19]: ch-store-retirement.md
[g4-20]: base-sync-2026-10-10.md
