#!/usr/bin/env bash
# Submit a one-shot GCP Batch job that runs cw's static name index catch-up outside the scan pipeline:
# `dt-cloud static-names runs add -c SCAN` (every published scan still pending through SCAN, in scan-id order,
# then the due carries), the coordinator on a small STANDARD VM, each stage its own Batch job as in the scan's
# `static-names` step (job/static-names.json). For a backlog longer than the scan's 6 h `static_names` task
# (2026-10-22: 8 scans behind, anchors past 4 h each): skip `static-names` in cw.yml while this runs, since two
# coordinators must never append at once.
#
#   job/cw-static-submit.sh 2026-10-22T1202   # catch up through that scan
#   DRY=1 job/cw-static-submit.sh <scan>      # print the body only
#   NO_MERGE=1 job/cw-static-submit.sh <scan> # skip the due carries (`-M`): a level-5 compaction is hours, so it
#                                             # runs on its own after (`dt-cloud static-names runs merge`)
#   MERGE=1 job/cw-static-submit.sh            # no append: the newest manifest's due carries, then the due
#                                             # compaction (`dt-cloud static-names runs merge`)
#
# Same image and SA as the scan job (cw-batch-submit.sh).
set -euo pipefail

MERGE=${MERGE:-}
if [ -n "$MERGE" ]; then
  if [ $# -ne 0 ]; then echo "usage: MERGE=1 $0" >&2; exit 2; fi
  SCAN=
else
  if [ $# -ne 1 ]; then echo "usage: $0 SCAN_ID" >&2; exit 2; fi
  SCAN=$1
fi
PROJECT=oa-internal-450019
REGION=us-east1
SA=cw-s3-job@$PROJECT.iam.gserviceaccount.com
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cw-s3-usage/scan:latest}
MACHINE=${MACHINE:-e2-standard-4}
KIND=catchup; [ -n "$MERGE" ] && KIND=merge
JOB_ID=${JOB_ID:-cw-sn-$KIND-$(date -u +%Y%m%d-%H%M%S)}

NO_MERGE=${NO_MERGE:-}
export PROJECT SA IMAGE MACHINE SCAN NO_MERGE MERGE
body=$(python3 <<'EOF'
import json, os, shlex
cmd = (
    "set -euo pipefail\n"
    "export STATIC_NAMES_PROFILE=/app/job/static-names.json\n"
    "set -x\n"
    + ("dt-cloud static-names runs merge\n" if os.environ['MERGE'] else
       f"dt-cloud static-names runs add -c {shlex.quote(os.environ['SCAN'])}{' -M' if os.environ['NO_MERGE'] else ''}\n")
    + "echo STATIC-CATCHUP-DONE"
)
print(json.dumps({
    "taskGroups": [{
        "taskCount": 1, "parallelism": 1,
        "taskSpec": {
            "runnables": [{"container": {"imageUri": os.environ["IMAGE"], "entrypoint": "bash", "commands": ["-c", cmd]}}],
            "maxRetryCount": 0,
            "maxRunDuration": f"{48 * 3600}s",
            "environment": {"variables": {"DISKY_LABELS": "app=disky,deployment=cw"}},
        },
    }],
    "allocationPolicy": {
        "instances": [{"policy": {"machineType": os.environ["MACHINE"], "provisioningModel": "STANDARD",
                                  "bootDisk": {"type": "pd-balanced", "sizeGb": "100"}}}],
        "serviceAccount": {"email": os.environ["SA"]},
    },
    "labels": {"app": "disky", "deployment": "cw", "component": "static-names"},
    "logsPolicy": {"destination": "CLOUD_LOGGING"},
}, indent=1))
EOF
)

if [ -n "${DRY:-}" ]; then echo "$body"; exit 0; fi
DISKY_LABELS=${DISKY_LABELS:-app=disky,deployment=cw} dt-cloud batch submit -c static-names -p "$PROJECT" -L "$REGION" "$JOB_ID" - <<<"$body" >/dev/null
if [ -n "$MERGE" ]; then echo "submitted $JOB_ID (static-names runs merge on $MACHINE)"
else echo "submitted $JOB_ID (static-names runs add -c $SCAN${NO_MERGE:+ -M} on $MACHINE)"; fi
echo "  gcloud batch jobs describe $JOB_ID --project $PROJECT --location $REGION --format='value(status.state)'"
