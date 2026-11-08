# Incremental R2 read-recency checkpoint

Status: implementation in progress, 2026-11-08. R2 CS3 is high priority; upstream `r/cloud` through `0a41e942` is merged as `5126d1de`. No archive deletion or new retention policy is applied by this work.

The collector is healthy: its 2026-11-08 00:13 UTC run has zero waiting windows, gaps or errors; 128/400 pages, 1,728 active windows and 3,844 frozen windows. The consumer still downloads both sets on every scan. Frozen windows are immutable: the collector moves an hour entry into `frozen` after it ages out of its reverification window.

## Read checkpoint

Add an opt-in consumer command that keeps one durable SQLite checkpoint in the deployment's existing data bucket. Closed history is one row per `(bucket, canonical path, operation)`: cumulative count, earliest and latest contributing timestamps, latest window and sampling flag. A cursor per bucket tracks incorporated frozen windows. Each update incorporates only newly frozen windows and reads the bounded active window set afresh, so a revised active window replaces its previous contribution rather than adding twice. The checkpoint is one atomically replaced object; its size follows distinct read keys rather than elapsed hours. It does not retain a growing list of processed window keys.

The output explicitly identifies itself as a read summary, not a general hourly traffic series. It preserves the read lens's per-path counts, last-read times, sampling flags and observation bounds. The existing hourly aggregation command stays available. A historical request older than the closed-history cutoff must refuse rather than silently use later counts; historical scan views remain in the path interval store.

Fetch retries follow the existing superseded-aggregate protocol: re-read state on a missing active key, and publish neither checkpoint nor output from a partial read. Bootstrap validation compares the summary with the full historical aggregation projected onto the read lens's fields. Tests cover unchanged updates, active replacement, active-to-frozen transition, empty windows, sampling, multiple buckets, canonical paths, backwards cuts, failure atomicity and a fixed workload's bounded row count.

## Rollout and later retirement

Run the first real build on Batch with bounded memory, compare against the current full aggregation, and publish the checkpoint only after validation. Enable the primary scan profile after CI and the comparison pass, then verify a manual scan and its serving probe. This creates derived data in the existing deployment; it does not mint grants or change IAM.

Collector `frozen` metadata and old archive aggregates remain until a separate handoff is implemented: a verified checkpoint watermark, bounded collector bookkeeping and an exact retirement list reviewed by Ryan. All deletions remain held. Age history and meta retirement adopt the existing shared implementations and receive parity plus cold/warm read benchmarks before any serving cutover.
