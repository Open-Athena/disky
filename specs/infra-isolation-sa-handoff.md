# Root/cloud: delegate deployment isolation preparation

Status: coordination request from CW, updated 2026-10-12; no live infrastructure changes made. Ryan explicitly asked: “root session main thread seems busy, can you ask it to do some of the above in SAs?” This requests delegation of preparation already underway in root, not parallel edits to deployment worktrees or authority for live cutovers. CW's initial inventory/additive preview/runbook is now in `specs/cw-infra-isolation.md`; eight creations, 35 existing resources unchanged. Root/shared helpers remain upstream-owned.

## Suggested SA split

1. **Resource ownership and access audit (read-only):** inventory the CW/GCS shared data and scratch buckets, publishing credentials, legacy cross-deployment IAM, schedules, image publishing and Pulumi ownership. Separate current source declarations from verified live state. Produce a resource → owner → consumers → intended owner map; identify which old grants are still needed. Do not read or print secret values. The older `batch-iac.md` describes a shared job account; current CW code declares dedicated job/dispatch accounts, so do not use the old spec as current live evidence.
2. **Isolation migration contract:** use that inventory to draft a phased spec and deployment handoffs. GCS retains its existing data/scratch buckets initially; CW gets dedicated ones and a deployment-scoped publishing credential. Include history/artifact references, copy verification, scheduler/site/meta config, rollback, a successful scheduled scan before revoking old access, and a distinct new R2 stack. Keep provider/region migration separate from isolation. Identify resource ownership so no two Pulumi states manage the same bucket or credential container.
3. **Shared helper and validation work, if needed:** implement generic IaC/config seams and exact tests in root-owned files only, with disjoint file ownership agreed before starting. Validate deterministic submitter specs, deployment-scoped credentials and output paths, and failure-safe publication. Keep provider-specific values in deployment branches; do not fork shared engine/site features.

Root should integrate/review SA results and coordinate sequencing. CW and GCS threads own their respective branch wiring, validation and cutovers; do not have root SAs independently modify their active worktrees. Follow their existing Pulumi preview-only gates; live `up`, credential changes, scheduler cutovers, revocations and deletion require the relevant authority. This request adds none.

## Current source evidence

- CW already declares separate job and dispatch accounts and its own scan/meta crons in `infra/gcp/__main__.py`.
- CW has bucket-wide `objectAdmin` grants on GCS-owned `oa-gcs-usage-dvx` and `oa-gcs-usage-scratch`.
- CW consumes the GCS-owned `cf-pages-token` container; `legacyJobAccess` can preserve GCS access to CW secrets/dispatch.
- GCS declares ownership of those buckets in its `infra/gcp/__main__.py`. Serving R2 buckets and D1 are already separate.

These are source observations, not a new live IAM audit. Relevant root specs: `oa-store-inventory.md`, `cloud-placement.md`, and `oa-decoupling.md`. The superseded `/na` hosting recommendation in CW's `marin-na.md` must not be revived: OA R2 is intended to have an independent deployment.

## Delivery

On October 12, Agent Mail initially delivered to the then-listed manual identity `disky-root@users-ryan-c-disky`. Root later confirmed its hook identity is `codex-disky-01a10725@users-ryan-c-disky`, a different inbox. CW acknowledged root's correction (message 7) and resent the full upstream UI/isolation handoff to the verified current identity as message 9, thread `cw-ui-isolation-oct12`. Delivery is confirmed; acknowledgement/implementation is separately tracked. CW registered as `codex-cw-s3-01a10727`; machine-wide mail bootstrap is handled by the separate `.codex` owner. Root can also pick this up with `$read cw` (pin the CW Codex target `01a10727-2e3b-7242-bd2e-4cb1a003e670` if the project alias is ambiguous).
