#!/usr/bin/env python3
"""The candidate image's smoke (`job/ship.sh`) as a Batch job spec: IMAGE SHA → stdout. Runnable 1 overrides the entrypoint and checks
the image's contents; runnable 2 keeps the image's ENTRYPOINT (the `bash -c 'exec $DISKY_ENTRYPOINT "$@"'` wrapper)
with `DISKY_ENTRYPOINT=dt-cloud`, so the wrapper itself runs `dt-cloud image-sha -e SHA`."""
import json, sys
image, sha = sys.argv[1:3]
check = f"""set -euxo pipefail
dt-cloud image-sha -e {sha}
disk-tree --help >/dev/null
dt-cloud scan head --help >/dev/null
dt-cloud monitoring-relay serve --help >/dev/null
test "$DT_NF_DIR" = /app/job/nf && test -f /app/job/nf/scan.nf
command -v nextflow && nextflow -version
node --version
wrangler --version
command -v time ps curl
test -f /app/dist/index.html && ls /app/dist | head
test -f /app/job/batch-submit.sh && grep -c DT_SCAN_IMAGE /app/job/batch-submit.sh
test -f /app/site/wrangler.toml
echo SMOKE-CONTENTS-OK"""
spec = {
    "taskGroups": [{
        "taskCount": 1,
        "taskSpec": {
            "runnables": [
                {"container": {"imageUri": image, "entrypoint": "/bin/bash", "commands": ["-c", check]}},
                {"container": {"imageUri": image, "commands": ["image-sha", "-e", sha]}},
            ],
            "environment": {"variables": {"DISKY_ENTRYPOINT": "dt-cloud"}},
            "computeResource": {"cpuMilli": 2000, "memoryMib": 4000},
            "maxRetryCount": 0,
            "maxRunDuration": "1800s",
        },
    }],
    "allocationPolicy": {
        "instances": [{"policy": {"machineType": "e2-standard-2", "provisioningModel": "STANDARD"}}],
        "serviceAccount": {"email": "gcs-usage-job@oa-internal-450019.iam.gserviceaccount.com"},
    },
    "logsPolicy": {"destination": "CLOUD_LOGGING"},
}
print(json.dumps(spec, indent=1))
