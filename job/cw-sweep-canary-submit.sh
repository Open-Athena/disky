#!/usr/bin/env bash
# Submit `dt-cloud plan-sweep canary` as a Batch job: does CAIOS enforce a delete's ETag precondition, on `DeleteObject`
# (`If-Match`) and per item on `DeleteObjects` (specs/delete-preconditions.md)? It writes, overwrites and deletes only its
# own keys under a fresh `disky-canary/conditional-delete/<run>/`, then removes every version of them. It runs with the
# sweep executor's credentials (Secret Manager refs, as the site's dispatch passes them), so no one handles their values.
#
#   job/cw-sweep-canary-submit.sh marin-us-west-04a      # the bucket to probe
#   DRY=1 job/cw-sweep-canary-submit.sh marin-us-west-04a  # print the spec only
#
# The answer is the task log's last lines (`single: … · ENFORCED`); the job fails when either isn't enforced.
set -euo pipefail
[ $# = 1 ] || { echo "usage: $0 BUCKET" >&2; exit 2; }
BUCKET=$1
PROJECT=oa-internal-450019
REGION=us-east1
export PROJECT BUCKET
export SA=cw-s3-job@$PROJECT.iam.gserviceaccount.com
export IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cw-s3-usage/scan:latest}
export ENDPOINT=${SWEEP_S3_ENDPOINT:-https://cwobject.com}
JOB_ID=${JOB_ID:-cw-sweep-canary-$(date -u +%Y%m%d-%H%M%S)}
spec=$(python3 - <<'EOF'
import json, os
e = os.environ
secret = lambda name: f"projects/{e['PROJECT']}/secrets/{name}/versions/latest"
print(json.dumps({
    "taskGroups": [{"taskCount": 1, "taskSpec": {
        "runnables": [{"container": {
            "imageUri": e["IMAGE"], "entrypoint": "/bin/bash",
            "commands": ["-c", f"dt-cloud plan-sweep canary -b {e['BUCKET']}"],
        }}],
        "computeResource": {"cpuMilli": 2000, "memoryMib": 4000},
        "maxRetryCount": 0, "maxRunDuration": "1800s",
        "environment": {
            "variables": {"SWEEP_S3_ENDPOINT": e["ENDPOINT"], "AWS_DEFAULT_REGION": "us-east-1", "AWS_EC2_METADATA_DISABLED": "true", "DT_S3_ADDRESSING_STYLE": "virtual"},
            "secretVariables": {"AWS_ACCESS_KEY_ID": secret("cw-s3-access-key-id"), "AWS_SECRET_ACCESS_KEY": secret("cw-s3-secret-access-key")},
        },
    }}],
    "allocationPolicy": {
        "instances": [{"policy": {"machineType": "e2-standard-2"}}],
        "serviceAccount": {"email": e["SA"]},
        "location": {"allowedLocations": ["regions/us-east1"]},
    },
    "logsPolicy": {"destination": "CLOUD_LOGGING"},
}, indent=2))
EOF
)
if [ -n "${DRY:-}" ]; then echo "$spec"; exit 0; fi
DISKY_LABELS=${DISKY_LABELS:-app=disky,deployment=cw} dt-cloud batch submit -c sweep-canary -p "$PROJECT" -L "$REGION" "$JOB_ID" - <<< "$spec"
