#!/usr/bin/env bash
# CoreWeave S3 scan job (GCP Batch). Chain:
#   1. bulk-list  s3://<bucket> via the CAIOS S3-compatible endpoint
#   2. import     listing shards -> canonical layer-2 parquet
#   3. webdata    layer-2 -> the site's tree/age/meta JSONs (the diff is
#                 served live from the index tiers — step 4b)
#   4. publish    JSONs to gs://$DATA/snapshots/cw/<id>/
#
# Why GCP Batch and not AWS Batch: the output lands in GCS (the site's Pages
# Function lists `snapshots/cw/` straight out of the bucket), the image and
# service account already exist here, and the listing is CoreWeave *egress*
# either way -- running it in AWS would just add a second cloud to operate.
#
# Sizing note: this job is far lighter than the GCS one. A full 92M-object
# scan measured 26 min / 1.1 GB RSS for the listing and 3:39 / 1.8 GB for the
# import, so it needs neither highmem nor local-SSD spill -- an n2-standard-8
# with a plain boot disk is enough. The bytes that matter are transient listing
# shards (~10 GB), not DuckDB spill.
#
# Env: SCAN_BUCKETS (space-separated, the first is the primary — the quota bucket,
# the sweep/lifecycle default; specs/done/cw-multi-bucket.md), SWEEP_BUCKET (the primary;
# defaults to SCAN_BUCKETS' first), SWEEP_S3_ENDPOINT (each also read under its old
# name, CW_BUCKETS / CW_BUCKET / CW_ENDPOINT), DATA_BUCKET, SNAP_ID (default: UTC
# YYYY-MM-DDTHHMM at listing start), LISTING_PROCS/WORKERS,
# AWS_ACCESS_KEY_ID/SECRET_ACCESS_KEY (injected from Secret Manager by
# cw-batch-submit.sh).
#
# The steps' bodies are `job/cw-step.sh` (shared with the Nextflow driver, `job/nf/cw.nf`); this is the one-VM driver:
# the steps in dependency order, with static-names (Batch stages this VM only waits on) running alongside
# publish-r2 → interval-store, both waited on before index-sync (specs/workflow-engine.md §2).
set -euxo pipefail
. "$(dirname "$0")/cw-env.sh"
# ERR: name the failed command for the record and the alert (`finish` posts it). The backfill matches `scan_err`'s
# xtrace line (`error_marker`), so its name and argument order are a contract. A step's own ERR text comes back
# through `$SR_ERR_FILE`.
export SR_ERR_FILE=${TMPDIR:-/tmp}/cw-step-err
rm -f "$SR_ERR_FILE"
on_exit() {
  local rc=$?
  if [ "$rc" != 0 ] && [ -s "$SR_ERR_FILE" ]; then SR_ERR=$(cat "$SR_ERR_FILE"); fi
  sr finish -x "$rc" ${SR_ERR:+-e "$SR_ERR"}; exit "$rc"
}
SR_ERR=""
trap 'scan_err $? $LINENO "$BASH_COMMAND"' ERR
trap on_exit EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
sr start -B -g "$GEN" "$SNAP_ID"
step() { bash job/cw-step.sh "$@"; }
cd "${APP_DIR:-/app}"

step bucket-check
for b in $BUCKETS; do step scan-bucket "$b"; done
step index

STATIC_PID=""
if [ "${STATIC_NAMES:-0}" = "1" ] && [ "${REPROC:-0}" != "1" ]; then
  step static-names &
  STATIC_PID=$!
fi
step publish-r2
step interval-store
if [ -n "$STATIC_PID" ]; then
  begin static-wait
  wait "$STATIC_PID" || echo "WARN: static-names stage exited $?" >&2
  phase static-wait
fi
step index-sync
for s in over-time index-gc warm-cache probe digest; do step "$s"; done

echo "CW-SCAN-JOB-DONE $SNAP_ID -> gs://$DATA/snapshots/cw/$SNAP_ID"
