#!/usr/bin/env bash
# A daily r2 scan's submitter. Cloud Scheduler (`oa-r2-usage-daily`, `oa-r2-usage-daily-meta`) runs a Cloud Run job
# (`oa-r2-usage-submit`, `oa-r2-usage-submit-meta`), which runs this (infra/gcp `BatchSubmitCron`: its text is the
# container's `bash -c`). `$BATCH_SPEC` is the head's Batch spec (`job/r2-scan plan -H job/profiles/<profile>`, rendered
# at `pulumi up`); the stack also sets `$PROJECT`, `$REGION`, `$SUBMIT_JOB` (the Cloud Run job), `$JOB_PREFIX` (the Batch
# job's name before its time) and `$SCAN_NAME` (what an alert calls the scan).
#
# The job is named for the UTC day (`<prefix>-YYYYMMDD`), and a job of that name already existing counts as submitted,
# so a Scheduler retry, a second execution, or a re-run by hand that day starts no second scan. Transient failures (429,
# 5xx, connection) are retried for `$SUBMIT_RETRY_S` (default 30 min). If the submit still fails, this posts to
# `$SLACK_ALERT_CHANNEL` (with `$SLACK_BOT_TOKEN`) and exits nonzero.
#
# A manual scan (`DT_TRIGGER=manual`: the site's "Run a scan", `/api/scan-runs/dispatch`, runs this job with that one
# override, the only variable the job lets a run change) is the same spec with `DT_TRIGGER=manual` in its env, named for
# the second (`<prefix>-YYYYMMDD-HHMMSS`), so it never meets the day's job; its alert says it was manual.
set -uo pipefail

: "${BATCH_SPEC:?the scan job spec (JSON), from the Cloud Run job env}"
: "${PROJECT:?}" "${REGION:?}" "${SUBMIT_JOB:?}" "${JOB_PREFIX:?}" "${SCAN_NAME:?}"
trigger=${DT_TRIGGER:-schedule}
spec=$(mktemp)
log=$(mktemp)
printf '%s' "$BATCH_SPEC" > "$spec"
case "$trigger" in
  schedule)
    template=$JOB_PREFIX-%Y%m%d
    batch_job=$(date -u "+$template")
    dt-cloud batch submit -c scan -p "$PROJECT" -L "$REGION" -t -e -r "${SUBMIT_RETRY_S:-1800}" "$template" "$spec" 2>&1 | tee "$log"
    rc=${PIPESTATUS[0]}
    ;;
  manual)
    batch_job=$(date -u "+$JOB_PREFIX-%Y%m%d-%H%M%S")
    # the cron's spec, its run recorded as manual (`scan_runs.trigger`); refused unless it carries the cron's trigger
    python3 - "$spec" <<'PY' || { rm -f "$spec" "$log"; exit 2; }
import json, sys
path = sys.argv[1]
spec = json.load(open(path))
envs = [g["taskSpec"]["environment"]["variables"] for g in spec["taskGroups"]]
if not envs or any(v.get("DT_TRIGGER") != "schedule" for v in envs):
    sys.exit("BATCH_SPEC: want DT_TRIGGER=schedule in every task group's env, to make it manual")
for v in envs:
    v["DT_TRIGGER"] = "manual"
json.dump(spec, open(path, "w"))
PY
    dt-cloud batch submit -c scan -p "$PROJECT" -L "$REGION" -e -r "${SUBMIT_RETRY_S:-1800}" "$batch_job" "$spec" 2>&1 | tee "$log"
    rc=${PIPESTATUS[0]}
    ;;
  *)
    echo "DT_TRIGGER=$trigger: want schedule (the cron) or manual" >&2
    rm -f "$spec" "$log"
    exit 2
    ;;
esac
if [ "$rc" -ne 0 ]; then
  why=$(grep -v '^note: ' "$log" | tail -n 1)
  logs="https://console.cloud.google.com/run/jobs/executions/details/$REGION/${CLOUD_RUN_EXECUTION:-unknown}/logs?project=$PROJECT"
  if [ "$trigger" = manual ]; then
    DISKY_NOTIFY=slack disk-tree notify -t "$SCAN_NAME (manual): not submitted" -u "$logs" -l "submitter logs" \
      "The manual scan started from /scans didn't start: Batch job $batch_job was not created (exit $rc). ${why:-no error output}
Start it again from /scans once Batch answers." || true
  else
    DISKY_NOTIFY=slack disk-tree notify -t "$SCAN_NAME: not submitted" -u "$logs" -l "submitter logs" \
      "No scan today: Batch job $batch_job was not created (exit $rc). ${why:-no error output}
Re-run once Batch answers (a no-op if the job exists): \`gcloud run jobs execute $SUBMIT_JOB --region $REGION --project $PROJECT\`" || true
  fi
fi
rm -f "$spec" "$log"
exit "$rc"
