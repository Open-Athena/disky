# Catch `hf` up with `cloud` (2026-10-25)

From the controller session. Ryan (via gcs, 10-25) asked whether every deployment has today's work. `hf` / `o/hf` is at 668164e2 (10-21), 258 commits behind `r/cloud`. Under parity-by-default that gap is open work.

## Do

1. Merge the local `cloud` branch into `hf` (`git merge refs/heads/cloud`, never a rebase; `cloud` = `r/cloud` = 28a5d3d5 or later). The merge must pass CI (`ghws <full sha>`) before anything ships. Preserve `hf`'s own deltas; if a conflict touches files `cloud` also changed, ask the controller.
2. **Cost labels are required now** (`63cc958d`, `specs/done/cost-labels-required.md`):
   - Every Batch submit through dt-cloud refuses a spec without `app`/`deployment`/`component`.
   - `scan head` refuses to start unless `DISKY_LABELS` (env or the profile's `env`) carries `app` + `deployment`.
   - `dt-cloud cost-labels` without `-c` raises.
   - hf's scheduler bodies already carry `DISKY_LABELS` (gcs checked, 10-25). Also check every driver or shell submit hf runs. `job/hf-batch-submit.sh` submits the head: switch its `gcloud batch jobs submit` to `dt-cloud batch submit -c scan JOB_ID SPEC.json`, then check that the cron body it prints (`PIN=1 DRY=1`) still matches what the scheduler runs. A scheduler body change is pulumi (`infra/gcp`), so that needs Ryan's go.
3. Ship it the usual way: rebuild `:hf`, `o` push, site deploy (hf's standing go). D1 migrations or pulumi still need Ryan's go. Verify with a manual scan, not the cron.
4. **Static-names compaction:** none of the 10-25 compaction work changes hf's behaviour beyond speed. hf's static names run on one machine (`static-names add|build`), so the Batch fan-outs (measure / anchor / fold tasks) don't apply. Its carries still use the faster census, `_closed_levels` and `rkeep`, and stay byte-identical.

## Report

The merged sha and its CI run; what shipped (image digest, site deploy); the manual scan's id and result; any `DISKY_LABELS` gaps found.
