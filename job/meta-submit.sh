#!/usr/bin/env bash
# Submit a scan of gcs's "data behind this site" store (`job/profiles/gcs-meta.yml`) as `batch-submit.sh`'s Nextflow
# head; `PIN=1 DRY=1` prints the `gcs-meta-snapshot-daily` cron body (infra/gcp).
PROFILE=/app/job/profiles/gcs-meta.yml JOB_ID=${JOB_ID:-gcs-meta-scan-$(date -u +%Y%m%d-%H%M%S)} exec bash "$(dirname "$0")/batch-submit.sh"
