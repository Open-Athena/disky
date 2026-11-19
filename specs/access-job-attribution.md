# Attributing bucket requests to jobs

Status: 2026-11-19. Phases 0–3 are built; gcs is rolling them out (phase 0 live; phase 1 in the hourly access image; phases 2–3 in the scan profile).

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

Each UTC day's hourly `iris.worker` / `iris.task`, reduced to `workers` `(hour, worker_id, tpu_name, gce_instance_name, zone, address, device_variant)` and `tasks` `(hour, task_id, owner, job, attempt_id, worker_id, n_samples)` (`owner` / `job`: the task id's user and top-level job; `n_samples`: its distinct rows that hour), into `access/jobs/<day>/{workers,tasks}.parquet` (`tasks` written last: its presence marks the day done).

- `job/finelog/` (gcs): its own image (own `pyproject` + `uv.lock` pinning `marin-finelog` + `marin-rigging` at Marin `10b2167aed28`), so Marin's dependencies never enter disky's lock. Pins are bumped deliberately; a Finelog format change fails loudly against the pinned client. Built by `job/finelog/build.sh` (candidate, then `promote` moves `:latest`).
- The catalog comes from Finelog's own client (`ObjectQueryClient.pin_catalog`). The data objects are read with plain gcsfs: rigging's cross-region guard charges an opened object's whole size (a footer scan of `iris.worker`'s 235 segments counted as their 31 GB, and the first backfill died on it), so the job counts every fetched byte itself (footers by range, then only the row groups whose `ts` statistics meet the day, only the needed columns) against `FINELOG_READ_BUDGET_GB` (default 5 per run). `ObjectQueryClient.query` isn't used: its DuckDB `register_filesystem` call rejects rigging's `CrossRegionGuardedFS` (worth reporting upstream).
- A day Finelog doesn't cover through its end is left for a later run.
- `[cloud]` `dt-cloud access jobs` / step `access-jobs` (generic: the image is the deployment's, `options.access-jobs.batch.image`; its contract is `-o ROOT DAY…`): submits the image as its own Batch job for the window's missing days (default the 2 before the scan's); not core, side, never blocks the scan; not waited on. A day before the newest still missing means an earlier job failed: it is resubmitted, the command exits 3 and the phase warns (the scan's run thread / `#gcs-admin`). gcs runs it in us-central1, beside Finelog's us-central2 bucket.

### 3. Join and report

`[cloud]` `dt-cloud access attribute -s DAY…`, run by the `access-requesters` step after its rollups (`hourly: 3`, `attribute: true`), so it never races the hourly re-roll: `requesters-hourly/<day>` ⋈ `hosts/` (requester IP at that hour → the VM holding it in the snapshot nearest the hour, within 3 h, never one created after the hour ended, so a reused ephemeral IP isn't misattributed) ⋈ `jobs/<day>` (VM internal IP → that hour's workers → tasks, a VM's requests split by its tasks' `n_samples`). Into `access/attributed/<day>.parquet`: the rollup's columns with `n` / bytes scaled by the share (they still sum to the day's), plus `vm`, `owner`, `job`, `via` (`task` | `vm`: a VM no task ran on | `ip`: no VM holds the IP). Redone only when its inputs changed (parquet metadata `disky.inputs`): a day's job tables land after that morning's scan (their job is async), so each day is attributed again, fully, by the next scan. `dt-cloud access readers -r gs://…/access/attributed -b owner,job` reports it; matched share = the `via` = `task` fraction.

Checked against the backfill join (`tmp/classb/attrib.py`) on Nov 17: per-owner $ agree within $1 and `n` sums to the rollup's.

Open: a digest line (top jobs by request cost); a `/access` view.

## Backfill (2026-11-11 … 18)

Hourly rollups were cut from the raw logs before they aged out (Nov 11 is missing hours 00–01). No host snapshots exist for that week; the IP → VM map comes from VMs still alive on Nov 17 and Nov 19, which covers 30–48% of TensorStore reads on Nov 11–15, 78% on Nov 16 and 88% on Nov 18. Backfilled numbers are labelled partial.
