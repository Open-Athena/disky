#!/usr/bin/env bash
# Submit the hf scan (job/hf-run.sh) to GCP Batch.
#
#   job/hf-batch-submit.sh              # submit, print the job id
#   DRY=1 job/hf-batch-submit.sh        # print the spec
#   PIN=1 DRY=1 job/hf-batch-submit.sh  # the canonical spec: the Cloud Scheduler body (infra/gcp generates it)
#   NF=scan job/hf-batch-submit.sh      # the Nextflow head (`dt-cloud scan head $PROFILE`, default job/profiles/hf.yml)
#                                       # instead of the one-VM job; `SNAP_ID=… GEN=…` resumes that scan's session
#
# One small on-demand VM: the listing is bound by the Hub's rate limit, not by the machine (job/hf-run.sh's sizing
# note). No GCS: the job writes R2 only, and scratches on the VM's boot disk.
#
# Secrets (Secret Manager containers from infra/gcp, values added by hand — specs/hf-deployment.md): the Hub read
# token, the job's D1 token and the CF account id, the R2 endpoint + read/write key pair, the alert bot's token.
set -euo pipefail

# PIN=1: drop every ambient override so the body is byte-identical whatever the caller's shell (`submitter_spec`
# runs this under a scrubbed env). Must precede the ${VAR:-default} knobs below; the python vars() honors PIN too.
if [ -n "${PIN:-}" ]; then unset IMAGE MACHINE MEMORY_MIB NF PROFILE; fi

PROJECT=oa-internal-450019
REGION=us-central1
SA=hf-usage-job@$PROJECT.iam.gserviceaccount.com
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/hf-usage/scan:hf}
# e2-standard-4 (4 vCPU / 16 GB), on demand: the listing waits on the Hub's 1000-requests-per-5-min window, and the
# import / sorts / static names over one org are minutes. Measure the first runs (/scans) and adjust.
MACHINE=${MACHINE:-e2-standard-4}
CPU_MILLI=$(( ${MACHINE##*-} * 1000 ))
MEMORY_MIB=${MEMORY_MIB:-14000}
JOB_ID=${JOB_ID:-hf-scan-$(date -u +%Y%m%d-%H%M%S)}

labels() {  # cost labels (specs/done/cost-labels.md): $DISKY_LABELS + component $1, as JSON; {} when unset
  python3 - "$1" <<'PY'
import json, os, sys
d = dict(i.strip().split("=", 1) for i in os.environ.get("DISKY_LABELS", "").split(",") if i.strip())
print(json.dumps({**d, "component": sys.argv[1]} if d else {}))
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
    # failure alerts: #gcs-usage-alerts (shared with the gcs and cw-s3 jobs)
    "SLACK_ALERT_CHANNEL": g("SLACK_ALERT_CHANNEL", "C0BTUNT3B5Z"),
}
if not pin:  # one-off overrides forwarded only for manual submits, never the cron spec
    for k in ["SNAP_ID", "GEN", "HF_THREADS", "MEM", "STORE_ROOT", "DT_RECORD", "DT_ALERTS"]:
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
          "commands": ["scan-run", "supervise", "--", "bash", "job/hf-run.sh"]
        }
      }],
      "computeResource": {"cpuMilli": $CPU_MILLI, "memoryMib": $MEMORY_MIB},
      "maxRetryCount": 0,
      "maxRunDuration": "10800s",
      "environment": {
        "variables": $(vars),
        "secretVariables": {
          "HF_TOKEN": "$(secret hf-usage-hf-token)",
          "CLOUDFLARE_API_TOKEN": "$(secret hf-usage-cf-token)",
          "CF_PAGES_TOKEN": "$(secret hf-usage-cf-pages-token)",
          "CLOUDFLARE_ACCOUNT_ID": "$(secret hf-usage-cf-account-id)",
          "R2_ENDPOINT": "$(secret hf-usage-r2-endpoint)",
          "R2_ACCESS_KEY_ID": "$(secret hf-usage-r2-access-key-id)",
          "R2_SECRET_ACCESS_KEY": "$(secret hf-usage-r2-secret-access-key)",
          "SLACK_BOT_TOKEN": "$(secret hf-usage-slack-bot-token)",
          "SITE_TOKEN": "$(secret hf-usage-job-grant)"
        }
      }
    }
  }],
  "allocationPolicy": {
    "instances": [{
      "policy": {
        "machineType": "$MACHINE",
        "provisioningModel": "STANDARD",
        "bootDisk": {"type": "pd-balanced", "sizeGb": "100"}
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

# The Nextflow head (specs/pipelines-everywhere.md) only drives the DAG: each step is a Batch task of its own, which
# fetches its own secrets (the profile's `secrets`), so the head gets a small VM and only what it uses itself: the
# scan-run record's D1 token + account id, and the failure alert's Slack token.
if [ "${NF:-0}" != 0 ]; then
  PROFILE=${PROFILE:-/app/job/profiles/hf.yml} python3 - "$spec" <<'PY'
import json, os, sys
p = sys.argv[1]; j = json.load(open(p))
t = j["taskGroups"][0]["taskSpec"]
c = t["runnables"][0]["container"]
c["entrypoint"] = "dt-cloud"
c["commands"] = ["scan", "head", os.environ["PROFILE"]]
t["computeResource"] = {"cpuMilli": 2000, "memoryMib": 7000}
t["maxRunDuration"] = "21600s"
keep = {"CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "SLACK_BOT_TOKEN"}
t["environment"]["secretVariables"] = {k: v for k, v in t["environment"]["secretVariables"].items() if k in keep}
pol = j["allocationPolicy"]["instances"][0]["policy"]
pol["machineType"] = "e2-standard-2"
pol["bootDisk"] = {"type": "pd-balanced", "sizeGb": "30"}
json.dump(j, open(p, "w"), indent=2)
PY
fi

if [ -n "${DRY:-}" ]; then cat "$spec"; rm -f "$spec"; exit 0; fi
gcloud batch jobs submit "$JOB_ID" --project "$PROJECT" --location "$REGION" --config "$spec" >&2
rm -f "$spec"
echo "$JOB_ID"
