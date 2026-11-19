# Attributing bucket requests to jobs

Status: proposed 2026-11-19. Phase 0 (the hourly rollup) is built; phases 1–3 are not.

## Why

The access rollups (`dt-cloud access requesters`, `access readers`) say what read a bucket by path, client library and requester IP, but not which job or person. GCS bills every request to the bucket's project and carries no per-request label. The marin buckets' request cost is dominated by training jobs (TensorStore reading `datakit/store`); a Marin investigation (2026-11-18) attributed it to one researcher's data-mixture sweeps by correlating the bucket's read rate with the sweeps' training speed (0.85). This makes that attribution direct, daily, per job.

## The chain

1. **Request → public IP, per hour.** `access requesters -H` (phase 0, built): the rollup per UTC hour, with `-D3` keeping the store a job reads (`datakit/store/<store>`), into `access/requesters-hourly/<day>.parquet`.
2. **Public IP → VM, per hour.** Requester IPs are the VMs' external IPs. Only the live TPU / Compute APIs report them; Cloud Asset Inventory's history keeps TPU nodes but not their external IPs. So it has to be snapshotted while the VM lives.
3. **VM → Iris task, per hour.** Finelog's `iris.worker` (worker → `tpu_name` / `gce_instance_name`, internal address) and `iris.task` (task → worker; `task_id` = `/<user>/<job>/<index>`), in Marin's Finelog object store (`gs://marin-us-central2/finelog/marin`).

## Phases

### 1. `dt-cloud access hosts` (hourly, generic)

Snapshot the VMs of configured projects (`options.access.hosts.projects`; gcs: `hai-gcp-models`): TPU nodes (all zones) and Compute instances, one row per network endpoint: `ts`, `project`, `zone`, `kind` (tpu | gce), `name`, `labels` (JSON), `internal_ip`, `external_ip`, `created`. Into `access/hosts/<YYYY-MM-DD>/<HH>.parquet`. Runs in the hourly access ingest (it's already hourly and has the job SA); never fatal to the ingest. IAM: the job SA holds `roles/tpu.viewer` + `roles/compute.viewer` on `hai-gcp-models` (granted 2026-11-19).

No Marin code; any deployment whose readers are its own VMs can use it.

### 2. `access-jobs` (daily, Marin-specific, its own image)

Hourly `iris.worker` / `iris.task` reduced to `(hour, worker_id, tpu_name, gce_instance_name, zone)` and `(hour, task_id, attempt_id, worker_id, n)`, into `access/jobs/<day>/{workers,tasks}.parquet`.

- Read with Marin's own client, `marin-finelog`'s `ObjectQueryClient` (client-side DuckDB over a table's stable catalog segments), not a reimplementation of Finelog's catalog format.
- Compartmentalized: `job/finelog/` is its own image (own `pyproject` + uv lock pinning exact `marin-finelog` and `marin-rigging`), so Marin's dependencies never enter disky's lock. Pins are bumped deliberately; a Finelog format change fails loudly against the pinned client.
- The scan's `access-jobs` step submits it as its own Batch job (as `vm-pack submit` does); not core, side, never blocks the scan; a failure posts to `#gcs-admin`; idempotent per day, so a later run catches up.

### 3. Join and report

`access readers -b user,job` (and `-b store`): hourly rollup ⋈ hosts (external IP at that hour → VM; a VM created after the hour doesn't match, so a reused ephemeral IP isn't misattributed) ⋈ jobs (VM → tasks at that hour; a VM running several tasks in an hour splits its requests by their sample counts). Unmatched requests stay as their IP / client rows, and the report states the matched share. Then a digest line: top jobs by request cost.

## Backfill (2026-11-11 … 18)

Hourly rollups were cut from the raw logs before they aged out (Nov 11 is missing hours 00–01). No host snapshots exist for that week; the IP → VM map comes from VMs still alive on Nov 17 and Nov 19, which covers 30–48% of TensorStore reads on Nov 11–15, 78% on Nov 16 and 88% on Nov 18. Backfilled numbers are labelled partial.
