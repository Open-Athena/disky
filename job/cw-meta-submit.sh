#!/usr/bin/env bash
# Submit a scan of cw's "data behind this site" store (`job/profiles/cw-meta.yml`) as `cw-batch-submit.sh`'s Nextflow
# head; `PIN=1 DRY=1` prints the `cw-meta-snapshot` cron body (infra/gcp).
PROFILE=/app/job/profiles/cw-meta.yml JOB_ID=${JOB_ID:-cw-meta-scan-$(date -u +%Y%m%d-%H%M%S)} exec bash "$(dirname "$0")/cw-batch-submit.sh"
