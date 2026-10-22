# HF listing speed as the Marin bucket grows

Research, 2026-10-21. Context: [Marin PR #10441] adds remote HF bucket transfers through `fsutil cp` / `mv`; [PR #10437] reduces checkpoint duplication before migration. This deployment already discovers new buckets in the namespace automatically.

The October 21 inventory listed 250,246 objects with 6,741 API requests in 42 minutes 10 seconds. This is the whole namespace, including existing Git repos. The rate limiter's 1,506 seconds of accumulated thread waits is not elapsed wall-clock wait time. Increasing the listing VM's CPU count does not by itself reduce API page count or lift the token's quota.

## Implemented

The bucket tree endpoint defaults to 1,000 entries and accepts up to 5,000, according to the current [Hub OpenAPI]. `bulk_hf.list_bucket` now explicitly requests 5,000 and follows the server's pagination links. The improvement is up to five times fewer bucket page requests; it is not a promise of five times faster end-to-end namespace scans. Repo requests and other API overhead remain. Shared commit: `390852b5`; 432 engine tests passed, with a two-page pagination regression fixture.

An additional Batch experiment found that expanded repo trees accept an explicit `limit=100`, despite the SDK documentation describing 50 entries per page. On a public model at one pinned commit, the 100-entry page matched two 50-entry pages exactly for every field the lister uses, and all 100 files retained their last-commit dates. A dataset also accepted 100 entries; 101 on the model and 1,000 on the dataset were rejected. The lister now requests 100 when expansion is enabled and keeps following the server's pagination links. This can halve expanded-tree page requests while preserving age accuracy; full namespace speedup is not yet measured.

## Next practical improvement

Cache repo file trees by immutable commit SHA. The current scanner requests per-file last-commit dates: [expanded repo tree pages] default to 50 entries, compared with 1,000 without expansion; the explicit 100-entry limit above improves that but still revisits unchanged trees. Unchanged repo SHAs should reuse their previously collected trees. Visibility, billing usage and any history residual must still refresh independently; a cache must not reuse old billing totals or overlook another ref's storage. Disabling per-file dates would make scans cheaper but would change the age data, so it is not enabled.

Cache the raw tree entries, before billing, privacy and fallback dates are applied, keyed by endpoint, repo kind/name, immutable SHA and cache-schema version. A missing per-file date still falls back to the freshly read repo date. Continue fresh discovery and authorization checks; a repo no longer visible must disappear even if a cached tree exists. Refresh `lfs-files` independently when all-ref history is enabled. Bound cache retention to active repo SHAs and a short recovery window, or use a TTL; retaining one cache entry for every scan would reintroduce the cs3 problem. With unchanged repo SHAs, the request budget becomes fresh repo metadata plus changed-tree pages and bucket pages, rather than every repo tree on every scan.

Bulk discovery metadata was also probed on Batch, one entry per repo kind. The live models, datasets and Spaces list endpoints all rejected `usedStorage` expansion with HTTP 400, using either repeated `expand` or `expand[]` query parameters. The current dataset/Spaces SDK reference lists that field, but the measured endpoint validation does not accept it. Reconciliation still needs an independent, fresh billing source.

There is a better supported route: [the official `hf repos ls` command] uses `HfApi.list_user_repos`, backed by `/api/organizations/<namespace>/settings/repositories`. The job token successfully read all 623 repos/buckets in one response with no next page; three sampled entries (two buckets and a model) matched individual storage totals and visibility exactly. Entries carry `id`, `type`, `storage`, `visibility` and `updatedAt`, but no commit SHA. Combining this billing summary with fresh paginated SHA metadata could remove individual billing-info requests. Full coverage and canonical-row parity still need validation before changing the scanner; the three-entry experiment is not an end-to-end speed benchmark. Source inspected locally at `huggingface/huggingface_hub` commit `e1f595dd7db4c993837e320a6d62ccdd1717dc2d` (`hf_api.py:list_user_repos`, `cli/repos.py:repo_list`). This is repo-level metadata, not a bulk per-object bucket inventory.

The job token was checked on Batch: both `/api/whoami-v2` and `/api/buckets/open-athena` returned `RateLimit-Policy: "fixed window";"api";q=1000;w=300`. Its actual current allowance is therefore 1,000 requests per five minutes, roughly 3.3 requests/second sustained. The 6,741-request inventory corresponds to about 34 minutes of sustained quota budget, consistent with its 42-minute wall time; this is not a strict minimum for a finite run because the starting window phase and remaining balance matter. [Published quotas] vary by plan and apply to the authenticated member; organization size does not establish the job token's entitlement. Ask HF whether this token has the intended entitlement before increasing concurrency. More workers can hide latency within that quota, but cannot remove it.

The same probe saw four buckets with no next page; `open-athena/marin` was absent, and its tree returned 404. A fresh metadata-only check on October 22 around 00:51 UTC still returned 404 with the job token. The PR declares creation through a manually operated stack, so this does not establish that the bucket has been created or that the inventory token can see it. Check the apply state and visibility before migration; the scanner automatically includes buckets that its token discovers.

## Large bucket inventory options

- Stream bucket pages into bounded listing shards before the corpus becomes very large. The current `list_bucket` accumulates a whole bucket's rows in a Python list, and `list_hf` schedules all units through an ordered executor map. Larger VMs can help that memory demand; they do not lift the API quota. Page streaming and bounded prefetch would remove that memory dependency. This is not implemented yet.
- The [S3 gateway] supports `ListObjectsV2`. There is no verified evidence yet that its quota or listing throughput exceeds the Hub API. A benchmark requires the gateway's separate AWS-style key pair, derived from an HF token; the Hub token itself is not the S3 key pair. Ask HF about its throughput envelope, `StartAfter`, and partitioning before switching clients.
- Prefix-parallel Hub listings may hide page latency, but partitions must be complete and disjoint. Concurrent writes/deletes require documented consistency semantics or reconciliation. A bigger machine cannot parallelize one cursor chain automatically.
- The [bucket event stream] supplies add/update/delete events with a short replay window, roughly 15 minutes. A continuous collector, or a poller running comfortably inside that window, could write a durable internal log and maintain inventory incrementally; a daily job cannot rely on yesterday's cursor. Bootstrap ordering, reconnect/reset recovery and periodic full reconciliation need explicit handling. A frequent poller can replay to `ready` and close; an initial full listing longer than the replay window needs a separate bootstrap strategy agreed with HF.
- [Webhooks] can truncate `updatedFiles` above 10,000 changed files. They are useful triggers, but do not alone provide a complete inventory or durable change log.
- A successful migration copy plan knows its destination objects and could seed a provisional inventory. Independent reconciliation must still cover other writers, replacements and deletions. Sorting newest-first and stopping at the prior scan would miss deletions.
- No official bulk inventory/export suitable for this workload was identified in the reviewed documentation. Ask HF whether one exists, or whether there is a longer-lived change cursor or inventory checkpoint.
- The published [audit-log event list] covers administration and some repository operations, rather than a documented stream of bucket object additions, updates and deletions. It therefore does not establish an alternative durable inventory feed.

## Transfer planning

The [Marin transfer planner] distinguishes ordinary copies from synchronization. A recursive copy enumerates the source with `find`; for CW → HF, that source is CW. It checks every destination file individually only when `no_clobber` is requested. Synchronization builds both manifests. The namespace inventory's slow expanded Git trees therefore do not directly measure the cost of planning a copy into a new HF bucket.

For a very large migration, two separate costs deserve measurement: the planner materializes its file/action lists, and `no_clobber` can produce one HF existence lookup per source file. A bulk destination manifest may be useful when copying many files, provided the overwrite/race policy is preserved. Small updates into a large destination may instead benefit from individual checks. The completed copy plan can also seed a provisional inventory, followed by independent reconciliation. No Marin code or transfer operation was changed by this research.

## Coordination

The current cloud and GCS work was read before implementing. No parallel implementation of the page-size change, repo-tree cache, or persistent bucket collector was found. The cloud handoff identifies the shared commits; deployment-specific history builds and retention remain with HF.

Follow-up reads covered Claude cloud `068d962e` through line 162054 / 2026-10-22 00:29:28 UTC and Claude GCS `c80118c6` through line 55694 / 00:30:29 UTC. Both bilateral read states were `BOTH` before advancing these cursors. Cloud adopted the history/retry/bucket-page commits and reconciled its duplicate indefinite-session-log implementation with GCS. GCS owns its large compaction trials and cloud owns the shared concurrent-append guard; neither is implementing the HF cache or bucket collector. The 100-entry repo-page and final unpublished-retry fixes have separate shared handoffs.

A `thrds` draft is in `slck/hf-bucket-inventory/hf-bucket-inventory.md`, addressed to `#oa-hugging-face`. It asks about bulk inventory, parallel listing consistency, S3 throughput and `StartAfter`, durable changes, and service-account quota. Nothing has been posted. Posting requires the user's review/edit and explicit approval, then the `thrds` workflow.

[Marin PR #10441]: https://github.com/marin-community/marin/pull/10441
[PR #10437]: https://github.com/marin-community/marin/pull/10437
[Hub OpenAPI]: https://huggingface.co/.well-known/openapi.json
[expanded repo tree pages]: https://huggingface.co/docs/huggingface_hub/package_reference/hf_api#huggingface_hub.HfApi.list_repo_tree
[Published quotas]: https://huggingface.co/docs/hub/rate-limits
[S3 gateway]: https://huggingface.co/docs/hub/storage-buckets-s3
[bucket event stream]: https://huggingface.co/docs/hub/storage-buckets#tracking-changes
[Webhooks]: https://huggingface.co/docs/hub/webhooks
[audit-log event list]: https://huggingface.co/docs/hub/audit-logs
[Marin transfer planner]: https://github.com/marin-community/marin/blob/dc27c942d0e2f9ae1434251d64b43bd6754e3bbb/lib/rigging/src/rigging/filesystem/transfer.py
[the official `hf repos ls` command]: https://huggingface.co/docs/huggingface_hub/en/guides/cli#list-repos
