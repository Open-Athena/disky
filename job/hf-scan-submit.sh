#!/usr/bin/env bash
# The daily scan's submitter. Cloud Scheduler `hf-usage-snapshot` (08:00 UTC) runs the Cloud Run job
# `hf-usage-snapshot-submit`, which runs this (infra/gcp `BatchSubmitCron`: its text is the container's `bash -c`).
# `$BATCH_SPEC` is the scan's Batch spec: `PIN=1 DRY=1 job/hf-batch-submit.sh`, rendered at `pulumi up`.
#
# The job is named for the UTC day (`hf-usage-snapshot-YYYYMMDD`), and a job of that name already existing counts as
# submitted, so a Scheduler retry, a second execution, or a re-run by hand that day starts no second scan. Transient
# failures (429, 5xx, connection) are retried for `$SUBMIT_RETRY_S` (default 30 min). If the submit still fails, this
# posts to #hf-admin (`$SLACK_ALERT_CHANNEL`, with `$SLACK_BOT_TOKEN`) and exits nonzero.
#
# A manual scan (`DT_TRIGGER=manual`: the site's "Run a scan", `/api/scan-runs/dispatch`, runs this job with that
# override and `DT_TRIGGER_BY`, the admin who started it: the only variables the job lets a run change) is the same spec
# with `DT_TRIGGER=manual` and `DT_TRIGGER_BY` (`scan_runs.trigger_by`; only an address or handle, else unrecorded and
# the scan still starts) in its env, named for the second (`hf-usage-snapshot-YYYYMMDD-HHMMSS`), so it never meets the day's job; its alert says it was manual.
# Unset (the cron), nothing here differs.
set -uo pipefail

PROJECT=oa-internal-450019
REGION=us-central1
JOB=hf-usage-snapshot-submit
trigger=${DT_TRIGGER:-schedule}

: "${BATCH_SPEC:?the scan job spec (JSON), from the Cloud Run job env}"
spec=$(mktemp)
log=$(mktemp)
printf '%s' "$BATCH_SPEC" > "$spec"
case "$trigger" in
  schedule)
    template=hf-usage-snapshot-%Y%m%d
    day_job=$(date -u "+$template")
    dt-cloud batch submit -c scan -p "$PROJECT" -L "$REGION" -t -e -r "${SUBMIT_RETRY_S:-1800}" "$template" "$spec" 2>&1 | tee "$log"
    rc=${PIPESTATUS[0]}
    ;;
  manual)
    day_job=$(date -u +hf-usage-snapshot-%Y%m%d-%H%M%S)
    # the cron's spec, its run recorded as manual (`scan_runs.trigger`, the job's `trigger` label); refused unless it
    # carries the cron's trigger
    python3 - "$spec" <<'PY' || { rm -f "$spec" "$log"; exit 2; }
import json, os, re, sys
path = sys.argv[1]
who = (os.environ.get("DT_TRIGGER_BY") or "").strip()
if who and not re.fullmatch(r"[\w.+@:'() -]{1,200}", who):  # dt_cloud.scan_runs.TRIGGER_BY_RE
    print("note: DT_TRIGGER_BY is not an address or handle; the run's starter goes unrecorded", file=sys.stderr)
    who = ""
spec = json.load(open(path))
envs = [g["taskSpec"]["environment"]["variables"] for g in spec["taskGroups"]]
if not envs or any(v.get("DT_TRIGGER") != "schedule" for v in envs):
    sys.exit("BATCH_SPEC: want DT_TRIGGER=schedule in every task group's env, to make it manual")
for v in envs:
    v["DT_TRIGGER"] = "manual"
    if who:
        v["DT_TRIGGER_BY"] = who
for labels in (spec.get("labels"), spec.get("allocationPolicy", {}).get("labels")):
    if labels is not None and "trigger" in labels:
        labels["trigger"] = "manual"
json.dump(spec, open(path, "w"))
PY
    dt-cloud batch submit -c scan -p "$PROJECT" -L "$REGION" -e -r "${SUBMIT_RETRY_S:-1800}" "$day_job" "$spec" 2>&1 | tee "$log"
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
    DISKY_NOTIFY=slack disk-tree notify -t "hf manual scan: not submitted" -u "$logs" -l "submitter logs" \
      "The manual scan started from /scans didn't start: Batch job $day_job was not created (exit $rc). ${why:-no error output}
Start it again from https://hf.oa.dev/scans once Batch answers." || true
  else
    DISKY_NOTIFY=slack disk-tree notify -t "hf daily scan: not submitted" -u "$logs" -l "submitter logs" \
      "No scan today: Batch job $day_job was not created (exit $rc). ${why:-no error output}
Re-run once Batch answers (a no-op if the job exists): \`gcloud run jobs execute $JOB --region $REGION --project $PROJECT\`" || true
  fi
fi
rm -f "$spec" "$log"
exit "$rc"
