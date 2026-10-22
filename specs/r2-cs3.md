# OA R2: constant-size steady state (CS3)

Status: 2026-10-22. Lean primary path-sort retention is deployed; the deployment as a whole has not reached CS3. CS3 means bounded redundant/rebuildable storage for a fixed source workload. Exact historical versions still grow with changes; retaining unlimited history cannot promise a fixed total byte count.

## Observed pipeline state

- Primary 07:15 UTC head `job-8ef7ae08-7e8e-4c73-a2e2-6d8b2562b846`, scan `2026-10-20T0716`: failed in `static_names`. Batch reported exit 50002, no longer receiving VM updates. Listing, index, publish and interval-store append completed. Post steps, including retirement, did not run. The task's final log was not uploaded; container output survives in Cloud Logging.
- Meta 08:15 UTC head `job-70b10e6c-665a-40ab-b227-d41530ffee65`: succeeded, about 10 minutes.
- Access collector's October 22 00:13 UTC invocation advanced `collected_through` to `2026-10-19T19:00Z`, 64 active hours and no recorded gaps. It drained 632,539 observed events from the original ~709K estimated-request hour in 318 pages, then hit `hour_over_cap` on the next hour: about 4,171,858 estimated requests, much larger than the 400-page budget. Collection remains blocked, with 51 hours waiting in that manifest.
- CI on site/deployment checkpoint `057031ee` passed.

## Storage and retention

| Artifact | Current behavior | Remaining work |
|---|---|---|
| Primary per-scan path sorts | Path-only staging on GCS, no new R2 copy; retire keeps one after both indexes hold the scan and parity passes | Restore the failed append/post sequence; keep checking deletion and manifest advancement |
| Raw object listings | Profile does not enable `retire.layer1` or a rolling-window policy | Review a bounded raw-listing policy and exact deletion plan before enabling it |
| Age pyramids, snapshot JSONs, D1 age pointers | Kept per scan; about 17 MB of published data in the verified recent scans | Replace with historical indexes or specify a retained window; do not delete files current readers require |
| Meta-store per-scan indexes | Separate per-scan serving; no interval store or executing retire step | Establish equivalent historical serving and verified retirement, or an explicit bounded retained window |
| Nextflow work/cache/reports | In the data bucket, which has no lifecycle; scratch bucket alone has seven-day expiry | Define a resume window and safe cleanup after successful publication |
| Access archives and aggregates | Seven-day raw lifecycle verified live on October 20; read-only per-key aggregates implemented, closed-hour rollup/retirement remains open | Remove full-history reads from each scan and bound closed-hour bookkeeping |
| Interval/static index history | Append and compact versions instead of a full tree per scan | Measure actual change growth separately from redundant copies |

Read-only GCS metadata measurement on 2026-10-20: `listing/` 1,839,539,914 bytes; `meta-l2/` 1,167,930 bytes; Nextflow `nf/work/` 4,201,366 bytes. These are selected live prefixes, not a total across GCS, R2 and soft-deleted generations.

## Immediate repair

The primary static step sets a 12 GB DuckDB limit but its hardcoded `small` label selected an 8 GB `e2-standard-2` VM. Yesterday's successful static task peaked at 4.6 GB RSS. This is a sizing mismatch; it does not establish OOM as the cause of exit 50002.

Shared fix `e07aa38a`: the pipeline supports `machines.static` (default remains `small`); the R2 primary profile selects `e2-standard-4`, four CPUs, a 14 GB scheduler memory request and a 100 GB work disk. The DuckDB limit remains 12 GB. Verification: 84 focused Python tests and the local Nextflow DAG/resume test pass. Manual Batch head `oa-r2-scan-manual-1020-static-size`, scan `2026-10-20T1202`, succeeded October 20 at 14:53 UTC, using image digest `sha256:4af771180389b54a68efb3c37f4080a4ed713382479832f6fcd523b16c9a02e7`. All eleven tasks completed, including post. Static-name catch-up took 2h27m and peaked at 12.6 GB RSS, confirming that the previous 8 GB machine was undersized. With Ryan's go, saved plan `tmp/r2-static-size-schedulers-1021-plan.json` was applied October 22 around 00:42 UTC: both scheduler submission bodies now use this tested image, 41 resources unchanged. Live primary/meta schedulers are enabled at 07:15/08:15 UTC respectively. Post verified retirement parity, removed two old GCS sorts (107,534,383 bytes), warmed caches, completed the serving probe and converged the digest.

Collector repair: Ryan approved increasing OA's `MAX_PAGES` from 200 to 400, retaining `MAX_HOURS=8`, `PAGE_SIZE=2000` and the existing conservative invocation budget. Applied October 20 at 12:11 UTC using saved plan `tmp/r2-access-cap-1020-plan.json`: one collector Worker updated, six resources unchanged, no lifecycle change. The live Worker settings confirm the new cap. The existing raw archive lifecycle was separately verified live at seven days.

The 12:13 invocation got past the old cap check but failed after writing 318 pages. GraphQL execution metrics report `exceededMemory` (about 200 MiB reported memory, 33 seconds CPU, 700 seconds wall time); its watermark did not advance. Stored Worker-log queries returned no events despite enabled, persisted invocation logs, so absence of a console error did not establish success. Metrics are saved in `tmp/r2-access-cap-analytics-1020.json`.

Memory fix: pack canonical ULID event IDs into an exact 128-bit hash table (compare all bits on collisions; retain exact string-set behavior for other ID formats), and index read rows by action then key without storing an extra composite copy of every long path. A 700,000-event/140,000-read-key regression crashes the old implementation and passes after the fix under a 96 MiB JS heap limit. All 57 collector tests, TypeScript checking and the OA dry-run bundle pass. Ryan approved this second Worker apply; deployed October 21 at 22:59 UTC using `tmp/r2-access-memory-1020-plan.json` (one Worker content update, six resources unchanged). The October 22 00:13 run cleared the original hour; the following hour exposed the larger cap issue above.

Further serialization fix: extending the regression to the former `JSON.stringify` plus whole-buffer SHA-256 step reproduced another memory failure even after compacting IDs. The private aggregate now emits bounded JSON chunks, hashes through `crypto.DigestStream`, and uploads through `FixedLengthStream` after measuring the exact encoded length; JSON bytes, digest and schema stay identical. The complete 700K-event accumulate/hash/upload regression passes under the 96 MiB JS heap cap, along with 62 tests, type checking and the OA bundle. With Ryan's go, `tmp/r2-access-stream-1021-plan.json` was applied October 22 around 00:42 UTC (one Worker update, six unchanged). The previous 57-test claim covered accumulation only; it did not establish finalization safety.

Next repair, local and awaiting a separate rollout go: OA opts into five-minute windows (`WINDOW_MINUTES=5`, `MAX_HOURS=32`, same 400-page cap and hourly cron). `VERIFY_HOURS=12` counts windows, preserving a nominal hour of reverification after the subhour switch; both new and verify work share the same page budget. Oversized fresh windows shrink to whole-minute pieces. The watermark saves completed windows across invocations; reverify preserves each exact end. A twelve-minute cooperative cutoff leaves time to publish before cron termination. Default hourly behavior remains for RAC. Existing hourly history is retained, and consumer-first deployment is required because old as-of state readers reject minute keys. Worker/resume/boundary/deadline and memory regressions pass (69 tests); Python reader/status tests pass (60); isolated infrastructure tests pass (11), and type checking/bundling pass. The Worker plan `tmp/r2-access-windows-1022-plan.json` changes one Worker and leaves six resources unchanged. Reader image `sha256:73292ac01d6883ead5f26e32547b68845410db6af1d9da4217f99f9407f1bbc7` built successfully (Cloud Build `f61311f7-9b39-40ef-b3a8-4c4f796d5e26`). Scheduler plan `tmp/r2-access-window-readers-1022-plan.json` changes only two submission bodies to that image, with 41 resources unchanged. No older Batch scans were active at the check; manual scan `oa-r2-scan-manual-1022-window-reader` was submitted at 01:04 UTC using the new image (UID `oa-r2-scan-manual-0f48005d-7ea5-4cf400`), completion pending. Apply the scheduler plan first and verify its live bodies, then the Worker plan. Neither this reader pin nor the window switch has been applied; no history deletion is authorized by this repair.

## Access-log volume

The initial sample was about 80K requests/hour and 1.7 GB/day of raw JSON. The October 19 18:00 UTC hour was about 709K estimated requests: 33% HEAD, 27% LIST, 19% PUT, 8% DELETE, 7% COPY and 7% GET (rounded). The following hour grew to 4.17M (~1,160 requests/second): 41% HEAD, 24% LIST, 13% PUT, 9% GET, 6% COPY and 6% DELETE. These are individual measured hours, not a daily average. Ingestion sampling is present, so weighted estimates exceed observed event rows. Pages of 2,000 events were about 2.6 MB; the earlier failed drain wrote 318 pages, roughly 0.8 GB, before OOM. Repeated attempts copy the same window again. October 21 inventory: 32.4 GB under `runs/` (14,424 objects, including manifests) versus 15.6 MB of published hour aggregates (71 objects). The aggregates are behind the raw archive, so these totals are not an equivalent-coverage compression ratio. The raw total includes retry overhead and is not a fresh daily ingest rate. The collector queries the API hourly; it does not receive a pushed hourly dump. Logs contain request metadata, not object contents.

[Lean scans], [deployment], [access compaction].

[Lean scans]: lean-scans.md
[deployment]: r2-deployment.md
[access compaction]: r2-access-compaction.md
