# Cost labels: required, not opt-in

Ryan (10-25): "everything any disky deploy does should be labeled … it should default on from US, and error if appropriate label md isn't set."

## Why

gcs's billing export (10-19 → 10-24) has many Batch jobs with no `deployment`/`component` label. Most were race and probe jobs, plus their helpers:
- `sn-compact-probe-*`, `sn-measure-shards-*`, `sn-race-c001-*`;
- `sn-measure-probe-*`, `sn-probe-*`;
- `gcs-iv-repair-*`, `gcs-iv-r2copy-*`;
- some `sn-anchors-*` / `sn-anchor-shard-*` helpers.

They show up only as `batch-job-id`, so a deployment's real cost has to be rebuilt by hand, job name by job name.

Root causes:
1. **Labels are opt-in.** `cost_labels.label_batch_spec` returns the spec unchanged when `$DISKY_LABELS` is unset, so a job submitted from an env without it is silently unlabeled. A race head (`compact-probe`) had no `DISKY_LABELS`, so every helper it fanned out was unlabeled too.
2. **The env doesn't reach grandchildren.** It's forwarded into a child job's task env only when `scan_run`/`scan_id` labels are present. A fan-out under a non-scan driver loses it.
3. **Shell submits bypass Python.** `gcloud batch jobs submit --config spec.json` in deployment `job/*.sh` scripts and ad-hoc race scripts goes around `label_batch_spec` entirely.

## Requirements

1. **Required labels.** Every Batch job any disky code creates must carry `app`, `deployment` and `component`, both on the job (`labels`) and on `allocationPolicy.labels`.
   - `cost_labels` / `label_batch_spec` raise (e.g. `MissingCostLabels`, naming the missing keys) when `$DISKY_LABELS` lacks `app` or `deployment`, or no component is given.
   - There is no unset-means-unlabeled path and no opt-out flag. Tests set the env.
   - Remove the module docstring's "Unset `DISKY_LABELS` = no labels at all" contract.
2. **One choke point.** `batch.submit_job` (and any other `jobs.create` call, e.g. the fan-out helpers in `static_fanout.py` and `compact-probe`'s seeding) validates the final spec right before the network call, and raises if a required label is missing on either level.
   - A test asserts that every module that creates Batch jobs goes through it, e.g. a grep-style test that no other `batch_v1` / `gcloud batch jobs submit` call site exists in `cloud/src`.
3. **Inheritance.** `label_batch_spec` always forwards `DISKY_LABELS` into the child's task env, not only when scan labels are present. Any job that may itself submit helpers (head, compaction, compact-probe, measure/anchor/write/fold shards) then labels its grandchildren identically.
   - The child's `component` stays its own; `stage`/`purpose` keep working as today.
4. **A CLI for shell submits.** `dt-cloud batch submit -c COMPONENT [-L REGION] [-p PROJECT] JOB_ID SPEC.json` labels the spec from `$DISKY_LABELS` + `-c`, validates it (requirement 1), and submits it.
   - The deployment branches switch their `gcloud batch jobs submit` lines to it. gcs will do `job/batch-submit.sh`, `job/static-names.sh`, `job/access-submit.sh` and its race tooling; cw/hf their own.
   - It is also the documented way to submit ad-hoc jobs (races, analysis), so they're attributable.
5. **Nextflow.** `scan head` refuses to start unless the profile env's `DISKY_LABELS` has `app` + `deployment`. `process.resourceLabels` already merges `params.labels`; make sure `params.labels` always includes them, plus `component` (e.g. `scan`) next to the existing `step`.
6. **Audit.** `dt-cloud cost-labels audit [-d DAYS] -t BILLING_TABLE` lists the window's Compute Engine cost lines that have a `batch-job-id` but no `deployment` label (job id, first day, net $) and exits nonzero if any are found.
   - It's read-only BigQuery. A deployment can run it in its digest or a weekly check.
   - Non-Batch VMs (people's dev boxes in a shared project) aren't disky's, so they're out of scope.

## Out of scope / notes

- **Cloud Build:** builds have tags, not billing labels. Its ~$1–2/day stays unattributed unless builds move to a labeled Batch job; not worth it now.
- **Buckets:** already labeled (`shared`/`data`, `cw-s3`/`data`).

## Done when

- `cloud` tests cover:
  - the missing-label raise;
  - the choke-point grep;
  - grandchild env inheritance;
  - the CLI round trip.
- After the deployment branches merge it, `cost-labels audit -d 2` over a day with a race is clean.
