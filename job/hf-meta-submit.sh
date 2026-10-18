#!/usr/bin/env bash
# Submit the meta store's scan head (job/profiles/hf-meta.yml) to GCP Batch: job/hf-batch-submit.sh's spec over
# that profile. `PIN=1 DRY=1` prints the `hf-usage-meta` cron body (infra/gcp generates it).
exec "$(dirname "$0")/hf-batch-submit.sh" /app/job/profiles/hf-meta.yml
