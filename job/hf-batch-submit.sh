#!/usr/bin/env bash
# Submit the hf scan to GCP Batch: the Nextflow head (`dt-cloud scan head $PROFILE`, default job/profiles/hf.yml;
# specs/pipelines-everywhere.md), each step a Batch task of its own.
#
#   job/hf-batch-submit.sh              # submit, print the job id
#   job/hf-batch-submit.sh /app/job/profiles/hf-meta.yml  # another head (the meta store: job/hf-meta-submit.sh)
#   DRY=1 job/hf-batch-submit.sh        # print the spec
#   PIN=1 DRY=1 job/hf-batch-submit.sh  # the canonical spec: the Cloud Scheduler body (infra/gcp generates it)
#   SNAP_ID=… GEN=… job/hf-batch-submit.sh  # resume that scan's session
#
# The head only drives the DAG: each step is a Batch task of its own, which fetches its own secrets (the profile's
# `secrets`; Secret Manager containers from infra/gcp, values added by hand — specs/hf-deployment.md), so the head gets
# a small VM and only what it uses itself: the scan-run record's D1 token + account id, and the failure alert's Slack
# token.
set -euo pipefail

# PIN=1: drop every ambient override so the body is byte-identical whatever the caller's shell (`submitter_spec`
# runs this under a scrubbed env). Must precede the ${VAR:-default} knobs below; the python vars() honors PIN too.
# A profile argument is not ambient: a pinned body may name its head's (job/hf-meta-submit.sh).
if [ -n "${PIN:-}" ]; then unset IMAGE PROFILE; fi

PROJECT=oa-internal-450019
REGION=us-central1
SA=hf-usage-job@$PROJECT.iam.gserviceaccount.com
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/hf-usage/scan:hf}
PROFILE=${1:-${PROFILE:-/app/job/profiles/hf.yml}}
stem=$(basename "$PROFILE" .yml)
JOB_ID=${JOB_ID:-$stem-scan-$(date -u +%Y%m%d-%H%M%S)}

# What started the run (`scan_runs.trigger`: /scans, and a scan's first run's picks the digest's reply scan): the cron body
# (PIN) is the schedule; any other submit is by hand. In the job's env (`DT_TRIGGER`) and its labels.
labels() {  # cost labels (specs/done/cost-labels.md): $DISKY_LABELS + component $1 + trigger, as JSON; {} when unset
  python3 - "$1" <<'PY'
import json, os, sys
d = dict(i.strip().split("=", 1) for i in os.environ.get("DISKY_LABELS", "").split(",") if i.strip())
trigger = "schedule" if os.environ.get("PIN") else "manual"
print(json.dumps({**d, "component": sys.argv[1], "trigger": trigger} if d else {}))
PY
}

vars() {
  python3 - <<'EOF'
import json, os
pin = bool(os.environ.get("PIN"))
g = (lambda k, d="": d) if pin else os.environ.get
v = {
    **({"DISKY_LABELS": os.environ["DISKY_LABELS"]} if os.environ.get("DISKY_LABELS") else {}),
    "HF_URI": g("HF_URI", "hf://open-athena"),
    "R2_BUCKET": g("R2_BUCKET", "oa-hf-usage-index"),
    "WORK_DIR": g("WORK_DIR", "/work"),
    # the usage digest thread: #hf-usage
    "SLACK_CHANNEL": g("SLACK_CHANNEL", "C0C9G547AP7"),
    # alerts (pipeline errors, session logging): #hf-admin
    "SLACK_ALERT_CHANNEL": g("SLACK_ALERT_CHANNEL", "C0C9J9VDPV5"),
    "DT_TRIGGER": "schedule" if pin else "manual",
}
if not pin:  # one-off overrides forwarded only for manual submits, never the cron spec
    for k in ["SNAP_ID", "GEN", "DT_RECORD", "DT_ALERTS"]:
        if k in os.environ:
            v[k] = os.environ[k]
print(json.dumps(v))
EOF
}

secret() { echo "projects/$PROJECT/secrets/$1/versions/latest"; }

spec=$(mktemp)
cat > "$spec" <<EOF
{
  "taskGroups": [{
    "taskCount": 1,
    "taskSpec": {
      "runnables": [{
        "container": {
          "imageUri": "$IMAGE",
          "entrypoint": "dt-cloud",
          "commands": ["scan", "head", "$PROFILE"]
        }
      }],
      "computeResource": {"cpuMilli": 2000, "memoryMib": 7000},
      "maxRetryCount": 0,
      "maxRunDuration": "21600s",
      "environment": {
        "variables": $(vars),
        "secretVariables": {
          "CLOUDFLARE_API_TOKEN": "$(secret hf-usage-cf-token)",
          "CLOUDFLARE_ACCOUNT_ID": "$(secret hf-usage-cf-account-id)",
          "SLACK_BOT_TOKEN": "$(secret hf-usage-slack-bot-token)"
        }
      }
    }
  }],
  "allocationPolicy": {
    "instances": [{
      "policy": {
        "machineType": "e2-standard-2",
        "provisioningModel": "STANDARD",
        "bootDisk": {"type": "pd-balanced", "sizeGb": "30"}
      }
    }],
    "serviceAccount": {"email": "$SA"},
    "location": {"allowedLocations": ["regions/$REGION"]},
    "labels": $(labels scan)
  },
  "labels": $(labels scan),
  "logsPolicy": {"destination": "CLOUD_LOGGING"}
}
EOF

if [ -n "${DRY:-}" ]; then cat "$spec"; rm -f "$spec"; exit 0; fi
# Through dt-cloud, never gcloud's own submit: a spec missing a required cost label is refused (specs/done/cost-labels-required.md).
dt-cloud batch submit -c scan -p "$PROJECT" -L "$REGION" "$JOB_ID" "$spec" >&2
rm -f "$spec"
echo "$JOB_ID"
