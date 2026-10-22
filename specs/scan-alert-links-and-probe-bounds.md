# Scan alert navigation and bounded static probes

Status: implemented and tested in CW on 2026-10-22; the combined isolated image was promoted to the production job tag after explicit approval. A resume of the latest failed primary scan is running; its head downloaded the approved digest and reused all six completed source-listing tasks from the Nextflow cache. Production completion remains unverified. No commits or pushes.

## Alert navigation

Scan failure and warning alerts retain their task-log link and add `scan` (the exact recorded run, not its task UID) and `all scans`. The extra links use `SITE_URL`, append the non-primary store path when applicable, and URL-encode the run id. A missing run id still permits the scans-list link; a missing site URL adds neither. The unscanned-bucket notice, emitted separately by the scan step, uses the same helper.

Shared changes: `disk_tree.notify.push.Notice.links` accepts additional `(label, url)` pairs, rendered appropriately for Slack, Discord and Pushover. `disk-tree notify -L LABEL URL` adds one, repeatably, retaining the existing single-link flags. `dt_cloud.scan_runs.scan_alert_links` constructs site links; `JobRun.alert` and `scan_steps.bucket_check` attach them. No live test messages were sent or old Slack messages edited. Existing running tasks keep their installed code.

Validation: 108 notification/wrapper/step tests pass, including exact outbound Slack payloads, other channel formats, repeatable CLI links, secondary-store paths and missing configuration. The isolated candidate overlays the current production image with only the four affected modules; installed source hashes and CLI imports were checked during its successful build.

## Static probe join state

`static_anchors.probe_prior_rows` bounded each shard read to a key batch, but joined it against the entire `probe_ranges` table. DuckDB builds an ASOF join over all those right-hand ranges on every batch. The new predicate restricts the right input to the same keys as the shard read. It changes neither interval merging nor selected object rows.

A profiling regression reproduces the former 22-range join state for each one-key batch, then verifies one range after the fix, with exact output rows retained. The anchor, hex and profile suites pass (36 passed, two skipped). A fixed synthetic comparison returned identical count/hash signatures and ran about 1.7 times faster; this is not a production runtime estimate. The production job tag now includes this three-line query change and the alert-link changes. Existing running containers retain their installed code; completion of the production retry is needed to validate the performance fix at scale.

## Production failure audit

The last fully successful primary scan was October 20 at 12:02 UTC. The seven following attempts through October 22 at 06:02 UTC failed or completed with failed phases. The latest attempt finished its listing, index and interval append, but its static-name coordinator failed when the anchor worker processing the oldest pending October 20 scan reached its four-hour runtime limit. This was not an expected healthy state. Appends must advance in scan order, so a repeated failure on the oldest scan blocks later static publication.

The static failures combine repeated probe work with backlog and carry work; the bounded join fix is not yet evidence that every pending carry and scan will complete. Earlier required-carry and phase-outcome fixes also exposed failures previously hidden behind successful job exits. Separately, over-time group sealing used retired per-scan files and was repaired and verified in a recovery job. A newly populated source bucket remains outside the explicit source configuration and is correctly reported by bucket-check; that coverage gap is still open. Do not describe all pipeline issues as resolved until the retry, serving checks and source coverage are verified.

Adopt the shared hunks and tests from `wt/cw`; preserve unrelated changes already present in the same source files. Production image digests, build/check logs, benchmark scripts/results, live scan evidence and approval state are recorded privately under CW's `tmp/`. No source bucket, IAM, schema, ETag migration, static-generation pointer or running job was changed by this work.
