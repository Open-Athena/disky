# Scan compute right-sizing

Status: manual-only implementation under validation, 2026-10-13; 262 targeted fixtures pass. Current test image is `sha256:8412db9602a6dcf7c5e33578a06bf727286c5c21791f736be0fc972ef9f82a6e` (Cloud Build `4e3c4f4d-4c6b-4faa-b96a-db97d2d786a3`, succeeded). The boot-disk-only E2 controller `gcs-usage-snapshot-split-e2-a-20261013-183900` has started and dispatched `sc-compute-2026-10-13-1300-9dd3c8b6ba5b8c1f`; aggregation runtime acceptance is pending. Earlier N2 attempts hit confirmed capacity errors across three zones; all four owned stalled jobs were stopped with metadata preserved locally and no storage data deleted. No daily Scheduler sizing change, production image promotion or hosting migration is part of this work.

## Evidence and approach

Validation capacity note: Compute operation logs confirmed `stockout` for the boot-disk-only `n2-standard-4` in `us-central1-c`, with no VM created. That fourth owned job was also stopped after saving its metadata. The current scratch attempt is `gcs-usage-snapshot-split-e2-a-20261013-183900` in `us-central1-a`, with `MACHINE=e2-standard-4` for the controller and isolated destinations below `scratch/scan-split-e2-a-20261013-183900/`. Compute's machine-type API confirms four vCPUs and 16,384 MiB; task limits and the aggregation worker's N2 shape remain unchanged. This controller override is a validation candidate, not a default rollout. The local reporting profile now uses Google's published E2 reference rate [from the price sheet][prices]; this is still a configured estimate, not billed spend. The active test image predates that reporting-profile addition. General availability guidance supports changing zones/configurations [when capacity is unavailable][capacity].

The successful manual scan `2026-10-13T1300` ran for 2h32m41s. Listing/access overlapped for about 26 minutes, aggregation took 35m40s, and search took about 81 minutes beside other post-publication work. The aggregation VM stayed allocated throughout; post-publication CPU was effectively idle. A controller split is a cost improvement, not yet a wall-time speedup. Search remains the principal latency target.

Do not shrink aggregation below its proven memory envelope: earlier 128-GiB attempts OOMed, and process memory can exceed the DuckDB limit substantially. The anchor worker also has measured memory requirements; fewer cores are not automatically safe if memory falls with them. Custom extended-memory instances need a separate price comparison, not a proportional-vCPU saving assumption.

## Opt-in implementation

`SPLIT_COMPUTE=1` in `job/batch-submit.sh` uses an `n2-standard-4` controller with a 14,000-MiB task limit, 8-GB DuckDB cap, four threads and a 100-GB boot disk, without a local SSD. Its temporary files live on the boot disk. Manual `ZONE=us-central1-c` can constrain controller/worker allocation when the regional scheduler picks an exhausted zone. The canonical `PIN=1` Scheduler spec ignores both overrides and retains its original shape/location.

The controller overlaps regional listing fan-out with a separate access worker (`n2-highmem-16`, 123,200 MiB, 48-GB DuckDB cap, 16 query threads, 750-GB local SSD). Access failure remains nonfatal and watermarks preserve resume behavior. After the barrier it dispatches aggregation/publication on `n2-highmem-32` (250,000 MiB, 100-GB DuckDB cap, 16 threads, 1,500-GB local SSD). This worker verifies all completed listings, stages inputs, builds/uploads the path index, publishes snapshot data, verifies the required R2 copies and exits. The controller continues the existing search/interval/D1/health/warm/digest/GC sequence on the small VM.

Workers require the controller's exact digest-pinned image. Stable names incorporate root UID, scan and stage; an existing job must match its immutable configuration before resumption. Zero automatic retries preserve the current aggregation behavior. Runtime secrets are never copied from `os.environ`: the worker inherits only the submission's Secret Manager references and explicit nonsecret scan context. Both workers carry root `scan_run`, actual `scan_id`, component/stage cost labels and a `SCAN_ROOT_UID` parent link. Job recording/backfill recognize the distinct physical runs rather than attributing them to the controller's machine.

The required R2 publication barrier stays inside aggregation: its failure prevents the controller from advancing visibility. Scratch validation requires both destinations below `scratch/`; it skips D1, R2 publication, serving pointers and digests. The split refuses unrelated sweep/gate/access-only/NOP modes rather than accidentally running their heavy work on the controller.

## Measurements and rollout gates

Scratch/gate alerts stay in task logs, not production Slack; failure exit status and recorded errors remain intact. The first scratch controller was intentionally stopped at 18:14 UTC after worker capacity exhaustion, but its old image posted a misleading failure alert under the successfully completed source scan's ID. Exact regression fixtures reproduce that leak and verify the isolation fix plus unchanged production alerting. This source fix is not present in the already-running immutable scratch image; no production scan failed as a result of that cancellation.

`PATH_INDEX_RESOURCE` and `ACCESS_RESOURCE` JSON log samples every 15 seconds expose CPU seconds, process RSS/peak RSS, Arrow allocations and cgroup memory/events. Access logs additionally identify bucket, chunk, CSV staging/parsing, rollup, read-size aggregation and upload; aggregation retains its existing detailed timing markers. These samples complement Batch assignment intervals and Cloud Monitoring VM metrics. CPU includes the monitored process's threads, not separately spawned processes; cgroup memory includes the container's cache/children where Linux exposes it.

1. Exact-config, retry/resume, secret-reference, cost-label, profile/backfill, scratch isolation and publication-barrier fixtures must pass. The canonical cron shape must remain unchanged.
2. Build a separate immutable test image; do not move `:latest`.
3. Run an archived-listing scratch reprocess with isolated destinations to validate controller dispatch, child allocation/mounts, aggregation exit and output parity. Reused directory caches make this a warm-input validation, not a cold fresh-scan timing or memory acceptance.
4. Validate a fresh end-to-end split scan, including access and post-publication orchestration on the small controller, before enabling the daily split. Compare CPU, RSS/cgroup peaks, complete serving coverage, elapsed time and observed compute estimates; include VM startup/shutdown and disk charges when actual billing exports become available.
5. Before daily enablement, add/validate bounded provisioning waits and owned-child cleanup on controller abort. Batch task runtime limits do not bound time spent waiting for VM capacity; a cancelled controller must not leave an untracked worker queued to allocate later. Current manual validation retains job metadata and stops its own stalled jobs explicitly.
6. Only then revise the canonical Scheduler shape. Keep the unsplit image/spec available for rollback. No smaller aggregation/anchor shape is accepted by this controller change.

Further speed work should use measured stage/substage CPU and IO: search/anchor critical-path work, aggregation sort/rollup parallelism and listing stragglers. Spot/retry policy and stage-specific machines are independent experiments with explicit publication and memory gates.

[capacity]: https://docs.cloud.google.com/compute/docs/troubleshooting/troubleshooting-resource-availability
[prices]: https://cloud.google.com/products/compute/pricing/general-purpose
