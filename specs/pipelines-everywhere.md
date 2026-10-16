# Every deployment's scan pipeline on one declared DAG

Status: plan, 2026-10-16. Phase 2 of [workflow-engine.md] (decided: Nextflow), which cw has proven on prod (§7 there). Ryan: "we need to prepare to move all deploys' pipelines to that or sthg similar. the ad hoc bash script 'WF mgmt' is not acceptable."

## 1. Where things stand

| deployment | runner today | lines | engine |
|---|---|---|---|
| cw-s3 | `job/nf/cw.nf` + `cw-head.sh` + `cw-step.sh` (cron on the head since 2026-10-16 18:00) | 101 + 74 + 308 | Nextflow, google-batch |
| gcs | `job/run.sh` | 635 | bash on one Batch VM |
| r2.oa.dev | `job/r2-scan` | 710 | bash on one Batch VM |
| hf | `job/hf-run.sh` | 225 | bash on one Batch VM |
| m3 | `infra/aws/ingest.sh` (per laptop capture) | 209 | bash on AWS |
| r2.rbw.sh demo | `.github/workflows/daily-ingest.yml` | 186 | GHA steps |

They share one core sequence and differ only in sources and optional steps:

```
bucket-check
list+import ×source  ─┐
                       ├→ index ─┬→ static-names ──────────────┐
                       │         └→ publish-r2 → interval-store ┴→ index-sync → over-time, index-gc, warm-cache, probe, digest
```

- gcs adds: listing fan-out, `access-ingest`, `lifecycle pull`, `sweep execute`, the gate, `healthcheck`.
- m3 runs per capture, not per schedule: `hosts resolve`, ingest of an uploaded capture, then the core from index on.
- hf lists with `bulk-list hf://`. r2.oa.dev and the demo list R2.

cw's prototype proves the engine. What it doesn't do yet is be reusable: its steps are bash, and its buckets, secrets, machines and session namespace are hardcoded for cw.

## 2. Shape

1. **Steps are `dt-cloud scan step <name> [arg]`, Python, not bash.** Each step is one idempotent command:
   - It reads the deployment's profile (§3) and the scan's identity (`SNAP_ID`, `GEN`, `DT_RUN_ID`).
   - It skips if its declared outputs exist.
   - It records its phase into `scan_runs`.
   
   `cw-step.sh`'s bodies, and the per-deployment scripts' phases, move into `dt_cloud/scan_steps.py`: one function per step, with the source kind (gcs | s3 | r2 | hf) a parameter of `list`. Deployment-only steps (gcs's access, lifecycle, sweep and gate; m3's capture) are steps like any other, enabled by the profile.
2. **One pipeline: `job/nf/scan.nf`.** It is the DAG above, with optional steps gated on `params.steps`, and fan-out over `params.sources`. No deployment gets its own `.nf`.
3. **One head: `dt-cloud scan head`, Python, replacing `cw-head.sh`.** It:
   - stamps `SNAP_ID`, `GEN` and `DT_JOB_START`;
   - runs `scan-run start`;
   - derives the session (uuid5 of `disky/<deployment>/scan/<scan>`) and writes the history line;
   - runs `nextflow run scan.nf -resume <session>` from the profile's params;
   - on TERM, stops Nextflow and sweeps (`batch-cancel -l nf-session=…`);
   - runs `scan-run finish` with the labelled cost.
4. **Config is data.** `job/nf/base.config` holds the generic process defaults: the entrypoint override, the bucket bind, `errorStrategy`, `queueSize` and the labels. The deployment's executor block (project, region, service account, image, machine per label) is generated from its profile by the head, so there is no per-deployment `.config` by hand.

## 3. The deployment profile

One file per deployment, `job/profiles/<deployment>.yml`, on that deployment's branch. Generic code reads only the keys; it has no deployment defaults:

```yaml
deployment: cw-s3
executor: {kind: google-batch, project: …, region: us-central1, service_account: …, image: …}   # | awsbatch | local
work: gs://<data bucket>/nf          # the work dir + cache (NXF_CLOUDCACHE_PATH)
sources:                             # fan-out of `list`; `kind` picks the lister
  - {uri: s3://marin-us-east-02a, machine: n2-standard-8}
  - {uri: s3://hero-checkpoints}
steps: [access-ingest, lifecycle]    # optional steps on top of the core
machines: {small: e2-standard-2, medium: e2-standard-4, big: n2-standard-8, post: e2-highmem-4}
secrets: {AWS_ACCESS_KEY_ID: cw-s3-access-key-id, …}   # fetched in-task (`secrets exec`)
env: {R2_BUCKET: …, STATIC_NAMES: …}
```

It replaces each script's top-of-file constants, `NF_SECRETS` / `NF_PASS`, and the cron's env.

## 4. Executors

- **google-batch**: gcs, cw, hf, r2.oa.dev. Proven on cw.
- **awsbatch**: m3, if its ingest stays on AWS. Unproven: the first probe is a 3-step pipeline, as cw did. The alternative is m3 on google-batch reading its capture uploads from R2/S3.
- **local**: the r2.rbw.sh demo in a GHA runner (a local work dir is fine there; `NXF_CLOUDCACHE_PATH` on R2 through its S3 API for resume), and the tests (a fixture scan through `scan.nf` with `executor: local`).

## 5. What moves out of the steps

`append_runner`'s stage machinery is a hand-built DAG inside one step: Batch submit/poll, skip-if-exists, the lease, `concurrently`. It drives static-names' stages and the interval store's ranges and merges. Once the core is on `scan.nf`, those stages become processes in the DAG with their own machine sizes, and `append_runner` keeps only the store logic (manifests, carries, prune). This step comes later and isn't needed for the migration.

## 6. Order

1. **Generic layer on `cloud`:**
   - `scan_steps.py` ports cw's steps first, then gcs's and hf's;
   - `dt-cloud scan head`, `scan.nf`, `base.config`;
   - a local-executor test that runs a fixture scan through the DAG end to end.
2. **cw on the generic layer.** It stays on its own `cw.nf` until then; the switch is a profile plus an image rebuild, and its output bytes must not change.
3. **hf**: the closest to cw; the hf session does it with Ryan's go.
4. **r2.oa.dev.**
5. **gcs**: the biggest, with listing fan-out and the deployment-only steps; done after a week of cw and hf on the layer.
6. **The r2.rbw.sh demo** (local executor in GHA).
7. **m3**: per-capture, and its executor decided by the §4 probe.

Each deployment's old script is deleted when its first week on the DAG is clean. Every move carries the baseline in [CLAUDE.md "Deployments: parity by default"] (scan-run records, alerts, warm-cache, probe, digest).

## 7. Open

- m3: awsbatch, or google-batch over its uploads (§4).
- Static-names / interval-store stages as DAG processes (§5): when, and whether `append_runner`'s lease survives.
- Whether `scan.nf` should be generated from a Python DAG definition (steps, needs, executor and outputs as data, as workflow-engine §5 Phase 0 proposed) rather than hand-written Groovy. Leaning: hand-written `scan.nf` stays small (~100 lines), and the profile carries the variation.

[workflow-engine.md]: workflow-engine.md
[CLAUDE.md "Deployments: parity by default"]: ../CLAUDE.md
