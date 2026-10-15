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
set -euxo pipefail

# Secrets arrive from Secret Manager verbatim, and a version created with
# `echo … | --data-file=-` carries a trailing newline (it broke the Discord
# digest for two days, 2026-09-16/17). Strip surrounding whitespace off every
# secret env var before anything (boto, curl, wrangler, the CLI) reads it.
# xtrace off for the block: `${!n}` would print the values.
{ set +x; } 2>/dev/null
for n in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY CLOUDFLARE_API_TOKEN DISCORD_BOT_TOKEN \
         DISCORD_GCS_USAGE_WEBHOOK SITE_TOKEN GCS_USAGE_TOKEN SLACK_BOT_TOKEN SLACK_WEBHOOK \
         CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET R2_ENDPOINT R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY; do
  if [ -n "${!n+set}" ]; then
    v=${!n}; v=${v#"${v%%[![:space:]]*}"}; v=${v%"${v##*[![:space:]]}"}
    export "$n=$v"
  fi
done
unset n v
# The site token's old name (`GCS_USAGE_TOKEN`) is what the scheduler body still
# passes until its next `pulumi up`; carry it under the new one.
if [ -z "${SITE_TOKEN+set}" ] && [ -n "${GCS_USAGE_TOKEN+set}" ]; then export SITE_TOKEN=$GCS_USAGE_TOKEN; fi
unset GCS_USAGE_TOKEN
# The R2 archive through blobfs (`R2_ARCHIVE`): R2's own credentials profile + `buckets.yml` entry.
. "$(dirname "$0")/r2-profile.sh"
set -x

# Deployment config for the shared CLI (cloud/src/dt_cloud/{index_footer,warm}.py).
export D1_DB_ID=${D1_DB_ID:-7f1e1326-b879-4ecd-8621-846621c24f36}   # oa-cw-s3-usage-db (site/wrangler.toml)
export D1_DB_NAME=${D1_DB_NAME:-oa-cw-s3-usage-db}
export INDEX_VARIANTS=${INDEX_VARIANTS:-path}                           # no user-sorted tiers here
export SITE_URL=${SITE_URL:-https://cw-s3.oa.dev}
export SNAPSHOTS_SUBDIR=${SNAPSHOTS_SUBDIR:-cw}                          # snapshots/cw/ (site/wrangler.toml says the same)
export LAYER2_PREFIX=${LAYER2_PREFIX:-'cw-l2/{scan}/'}   # this store's layer-2 dir (`dt-cloud publish-r2 -l`; the base's is listing/{scan}/index/)
export WARM_PATHS=${WARM_PATHS:-",marin-us-east-02a,marin-us-east-02a/marin,marin-us-east-02a/tmp,marin-us-east-02a/iris,hero-checkpoints,hero-checkpoints/tmp,hero-checkpoints/marin,marin-us-east-06a,marin-us-east-06a/marin,rhoarnet-us-east-08a,marin-us-west-04a"}

# Every bucket in the scan; the primary first. The scheduler body sets only
# the primary, so the list's default here IS the deployment's.
BUCKETS=${SCAN_BUCKETS:-${CW_BUCKETS:-"marin-us-east-02a hero-checkpoints marin-us-east-06a rhoarnet-us-east-08a marin-us-west-04a"}}
BUCKET=${SWEEP_BUCKET:-${CW_BUCKET:-${BUCKETS%% *}}}
ENDPOINT=${SWEEP_S3_ENDPOINT:-${CW_ENDPOINT:-https://cwobject.com}}
export SCAN_BUCKETS=$BUCKETS SWEEP_BUCKET=$BUCKET SWEEP_S3_ENDPOINT=$ENDPOINT
unset CW_BUCKETS CW_BUCKET CW_ENDPOINT
DATA=${DATA_BUCKET:-oa-cw-s3-usage-data}
export DATA_BUCKET=$DATA
PROCS=${LISTING_PROCS:-$(nproc)}
WORKERS=${LISTING_WORKERS:-8}
# Snapshot ids are sub-daily: the bucket can move >40 TiB between morning and
# evening during a cleanup push, so a date-only id would silently overwrite.
SNAP_ID=${SNAP_ID:-$(date -u +%Y-%m-%dT%H%M)}
DATE=${SNAP_ID%%T*}
GEN=${GEN:-$(date -u +%Y%m%dT%H%M%SZ)}  # this run's index generation (step 4b), stamped once at start

# Scan-run record + failure alerts (specs/scan-job-wrapper.md): `dt-cloud scan-run` prints each PHASE line (timed
# from DT_JOB_START), records the run in D1 (`scan_runs`, shown at /scans; the index-sync credentials) and, when the
# job fails, posts one alert to SLACK_ALERT_CHANNEL through `disk-tree notify`'s slack channel, its text redacted of
# the profile's `secrets` (job/scan-runs.json, which also says what each phase wrote). It records only under Batch
# (BATCH_JOB_UID is the run id), not for a REPROC run, and never fails the job; xtrace is off around it so the
# tokens in its env never reach the log. Alerting was added after the 8/26–8/28 crash-loop on a missing script
# went unnoticed for two days.
export SCAN_RUNS_PROFILE=${SCAN_RUNS_PROFILE:-/app/job/scan-runs.json}
export DT_JOB_START=${DT_JOB_START:-$(date +%s)}
if [ "${REPROC:-0}" = "1" ]; then export DT_RECORD=off; fi
sr() { { set +x; } 2>/dev/null; local rc=0; dt-cloud scan-run "$@" || rc=$?; set -x; return "$rc"; }
begin() { sr phase-begin "$1"; }
phase() { sr phase-end "$1" ${2:+-q "$2"}; }
# ERR: name the failed command for the record and the alert (`finish` posts it). The backfill matches this
# function's xtrace line (`error_marker`), so its name and argument order are a contract.
scan_err() { { set +x; } 2>/dev/null; SR_ERR="exit $1: $3 (cw-run.sh:$2)"; }
on_exit() { local rc=$?; sr finish -x "$rc" ${SR_ERR:+-e "$SR_ERR"}; exit "$rc"; }
SR_ERR=""
trap 'scan_err $? $LINENO "$BASH_COMMAND"' ERR
trap on_exit EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
sr start -B -g "$GEN" "$SNAP_ID"
begin bucket-check

# Every bucket the CAIOS key can list is either scanned or empty. A non-empty
# one outside SCAN_BUCKETS (a new zone's bucket) is billed storage the site
# would silently miss, so it alerts each run until it's added. Never fatal (an
# empty bucket can't be scanned: bulk-list writes no shards for it). CAIOS
# rejects path-style object calls (`PathStyleRequestNotAllowed`), so the client
# is virtual-hosted, as `sweep.py` / `bulk_s3.py` build theirs. A failed check
# alerts too, naming the step and the error, never a bucket list.
UNSCANNED=$(python3 - "$ENDPOINT" $BUCKETS <<'PY' || echo "check failed: python exited $?"
import sys, boto3
from botocore.config import Config
endpoint, *scanned = sys.argv[1:]
s3 = boto3.client("s3", endpoint_url=endpoint, config=Config(s3={"addressing_style": "virtual"}))
try:
    names = sorted(b["Name"] for b in s3.list_buckets()["Buckets"])
except Exception as e:
    sys.exit(print(f"check failed: ListBuckets: {type(e).__name__}: {e}"))
out = []
for n in names:
    if n in scanned:
        continue
    try:
        if s3.list_objects_v2(Bucket=n, MaxKeys=1).get("KeyCount"):
            out.append(n)
    except Exception as e:
        out.append(f"{n} (check failed: ListObjectsV2: {type(e).__name__}: {e})")
print(" ".join(out))
PY
)
phase bucket-check
if [ -n "$UNSCANNED" ]; then
  echo "WARN: unscanned-bucket check: $UNSCANNED" >&2
  { set +x; } 2>/dev/null
  disk-tree notify -t "⚠️ cw-s3 $SNAP_ID" "non-empty CAIOS bucket(s) not in SCAN_BUCKETS: \`$UNSCANNED\`" \
    || echo "WARN: unscanned-bucket alert not sent (exit $?)" >&2
  set -x
fi

WORK=${WORK_DIR:-/stage/cw-$SNAP_ID}
mkdir -p "$WORK"
cd /app

# The lister opens a socket per concurrent range; the default 1024 soft limit
# is well under PROCS*WORKERS*keep-alive and surfaces as opaque connection
# resets partway through a multi-hour listing.
ulimit -n "$(ulimit -Hn)" 2>/dev/null || ulimit -n 65536

# 1 + 2, per bucket: listing, then layer-2. One listing dir and one layer-2
# root per bucket (paths stay bucket-relative; the bucket becomes the path's
# first segment at index/webdata time). `-x clear` starts from a clean output
# dir so a retried task never merges shards from a half-finished previous
# attempt. Import: `-j` is CPU-bound over the k-way merge; the shards' row
# groups are bounded at write time (see find/bulk.py) so each merge source
# decodes one 64K group rather than the whole shard -- that bound is what
# keeps this in ~2 GB.
SRC=()  # <bucket>=<layer-2>, in SCAN_BUCKETS order
for b in $BUCKETS; do
  begin "list $b"
  disk-tree bulk-list -a "s3://$b" -E "$ENDPOINT" \
    -o "$WORK/listing/$b" -P "$PROCS" -w "$WORKERS" -x clear
  phase "list $b"
  begin "import $b"
  env DISK_TREE_ROOT="$WORK/l2/$b" \
    disk-tree import -e stream -j "${IMPORT_JOBS:-$(nproc)}" -m -p storage_class_id \
      -s s3 -t "${DATE}T00:00:00+00:00" -l "$WORK/listing/$b/shard-*.parquet" -b "$b"
  SRC+=("$b=$(ls "$WORK/l2/$b"/scans/*.parquet | head -1)")
  phase "import $b"
done

# 3. Site JSONs: the store root wraps one node per bucket.
begin webdata
python job/cw-webdata.py "$WORK/web" "${SRC[@]}" -l "Marin CoreWeave" -a "$DATE"
phase webdata

# 4. Publish. Written last and all at once: the site's scan list is derived by
# listing this prefix, so a partially-uploaded snapshot would show up in the
# dropdown as a broken entry.
begin publish
DEST="/gcs/$DATA/snapshots/cw/$SNAP_ID"
mkdir -p "$DEST"
cp "$WORK"/web/*.json "$DEST/"
# Each bucket's lifecycle rules in force at this scan (Marin's tmp/ttl TTLs, the
# abort-MPU rule, the primary's noncurrent-version GC): snapshotted next to the
# scan as `{<bucket>: Rules[]}` so the site can show them and later infer their
# effects. `job/cw-lifecycle.json` is the primary's intended state (`dt-cloud
# lifecycle diff|push`); a pull never fails the scan.
LB=(); for b in $BUCKETS; do LB+=(-b "$b"); done
dt-cloud lifecycle pull "${LB[@]}" -o "$DEST/lifecycle.json" || echo "WARN: lifecycle pull failed" >&2
phase publish

# Keep the canonical layer-2 parquets too -- they're the input to every ad-hoc
# question ("what grew?", "what's idle?") that the JSONs can't answer.
begin layer-2
mkdir -p "/gcs/$DATA/cw-l2/$SNAP_ID"
for s in "${SRC[@]}"; do cp "${s#*=}" "/gcs/$DATA/cw-l2/$SNAP_ID/${s%%=*}.parquet"; done
phase layer-2

# 4b. Index tiers (specs/view-serving.md, ported from gcs): the site's
# /api/subtree, /api/diff and /api/series read row-group-pruned ranges of
# `path-index[-coarse<E>].parquet` — the layer-2's dir rows in the site's
# column contract, 8k-row groups, plus coarse tiers by absolute byte floor.
# Each run writes its tiers under a fresh generation dir and never overwrites
# a file D1 points at; `index-sync` lands the footers in D1 and flips the
# pointer last, so a reader sees the old complete set or the new one.
INDEX_KEY="cw-l2/$SNAP_ID/index/$GEN"
begin index-write
dt-cloud index-write -m "${DUCKDB_MEM:-16GB}" -t "${IMPORT_JOBS:-$(nproc)}" -o "$WORK/index" "${SRC[@]}"
mkdir -p "/gcs/$DATA/$INDEX_KEY"
cp "$WORK"/index/*.parquet "/gcs/$DATA/$INDEX_KEY/"  # path-index + coarse tiers + age-index
phase index-write
# 4b'. The static name index (specs/static-append.md): append this scan (and any earlier one still pending, in
# scan-id order) to cw's generation as a run — Batch stages, then R2, then `manifests/<scan>.json` — so the map's
# `?f=` filter and /names answer it statically. On when STATIC_NAMES=1 (the cron sets it, with STATIC_NAMES_IMAGE =
# this job's image): the stages run as this job's account (`cw-s3-job`, cw's profile; it holds the scratch bucket).
# See specs/cw-static-names.md. It reads the index tiers just copied (found by their GCS layout, not D1), and its
# stages are separate Batch jobs this VM only waits on, so it starts here in the background (an `overlapped` phase
# in job/scan-runs.json, recorded from when it began) and `index-sync` below waits for it to end, however long it
# takes: a scan is listed once it is searchable, as on gcs (specs/search-box-no-refusals.md §3.6). A failed append
# still lists the scan (its filter reads "not indexed" for that scan only). Never fatal: a scan the index doesn't
# hold is caught up by a later run's `-c`. Exit 3 = not the next scan yet.
STATIC_PID=""
if [ "${STATIC_NAMES:-0}" = "1" ] && [ "${REPROC:-0}" != "1" ]; then
  (
    begin static-names
    STATIC_NAMES_PROFILE=/app/job/static-names.json dt-cloud static-names runs add -c "$SNAP_ID" \
      || echo "WARN: static-names runs add failed for $SNAP_ID (exit $?)" >&2
    phase static-names overlapped
  ) &
  STATIC_PID=$!
  begin static-wait
  wait "$STATIC_PID" || echo "WARN: static-names stage exited $?" >&2
  phase static-wait
fi

# 4c. Publish this scan's served subset to R2 (specs/done/r2-serving-migration.md):
# the snapshot JSONs + the layer-2 dir (parquets, index tiers, manifests) the
# site reads through its `STORE_*` seam, colocated with the Worker instead of
# fetched cross-provider from GCS. Idempotent (size + md5), so a re-run or a
# backfill over old scans only moves what's missing. Must precede the warm-up
# below: once the site serves from R2, the warm has to find the scan there.
# Skipped without the R2 env (the `cw-s3-r2-*` Secret Manager secrets +
# `R2_BUCKET`, from `cw-batch-submit.sh`); the site keeps serving GCS then.
begin publish-r2
if [ -n "${R2_ENDPOINT:+set}" ] && [ -n "${R2_BUCKET:-}" ]; then  # `:+set`: xtrace must not print the creds
  # The canonical per-bucket listings (layer-2) go too: R2 is the archive the later
  # one-shots (reindex, recompress, REPROC) read, GCS only the job's scratch.
  # `-a`: every `index/<gen>/` under the scan, not D1's (the scan is listed in D1 after, 4c'').
  dt-cloud publish-r2 -a "$SNAP_ID" \
    || echo "WARN: publish-r2 failed for $SNAP_ID (the site keeps serving GCS)" >&2
else
  echo "no R2_ENDPOINT/R2_BUCKET — skipping publish-r2" >&2
fi
phase publish-r2

# 4c'. Interval store (specs/interval-store.md): append this scan, and any earlier one still pending, to cw's
# change-interval path store on R2 as a run beside the store's current generation (`interval-store/CURRENT.json`),
# then the binary counter's due carries, pruning the runs they fold, and every 2^5 scans a compaction into a new
# generation, the pointer moved with it (§2.7 "Compaction"; the site follows the pointer, cw pins none). One
# machine, reading the scans' `path` sorts from R2 (4c just published this one's). The site reads it under
# `PATH_STORE = "intervals"`; a scan the store doesn't hold reads per-scan. Never fatal (`-W`): a failure or the
# timeout warns, and the next run's `add` catches up. Skipped while the store has no generation
# (`job/cw-interval-submit.sh build`).
if [ "${INTERVAL_STORE_APPEND:-1}" = "1" ] && [ -n "${R2_ARCHIVE:-}" ] && [ "${REPROC:-0}" != "1" ]; then
  IV_GEN=$(dt-cloud interval-store current -o "$R2_ARCHIVE") || { echo "WARN: interval-store current failed: not appending $SNAP_ID" >&2; IV_GEN=-; }
  if [ "$IV_GEN" = - ]; then :
  elif [ -n "$IV_GEN" ]; then
    sr phase interval-store -W -T "${INTERVAL_STORE_TIMEOUT:-90m}" -m "the scan reads per-scan until a later run appends it" \
      -- dt-cloud interval-store add -L 'cw-l2/{id}/index/{gen}/path-index.parquet' -s "$R2_ARCHIVE" -o "$R2_ARCHIVE" \
         -w "$WORK/interval-store" -M "${INTERVAL_STORE_MEM:-12GB}" -p "$(nproc)"
  else
    echo "no interval store on R2 yet (job/cw-interval-submit.sh build): not appending $SNAP_ID" >&2
  fi
fi

# 4c''. List the scan: its footers to D1 (`index-sync`), last of the serving steps, so a listed scan is whole —
# searchable (4b', above), on R2 (4c) and in the interval store (4c') — as gcs lists it. What follows (over-time
# groups, GC, the cache warm-up, the probe, the digest) reads the listed scan.
begin index-sync
if [ -n "${CLOUDFLARE_API_TOKEN:+set}" ] && [ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then  # `:+set`: xtrace must not print the token
  dt-cloud index-sync -d "/gcs/$DATA/$INDEX_KEY" -g "$GEN" -k "$INDEX_KEY" -M "/gcs/$DATA/snapshots/cw" "$SNAP_ID" \
    || echo "WARN: index-sync failed (the site keeps serving the previous generation)" >&2
  dt-cloud index-gc "$SNAP_ID" || echo "WARN: index-gc failed" >&2
else
  echo "WARN: no CLOUDFLARE_API_TOKEN/ACCOUNT_ID — tiers published but not synced (scan unlisted for the index reader)" >&2
fi
phase index-sync

# 4d. Over-time index groups (specs/obs-axis-indexing.md Phase 1): every full
# K-scan run of indexed scans not yet in the manifest becomes one sealed
# multi-scan group; `/api/series` then reads ⌈N/K⌉ pruned groups instead of
# one point per scan. The stage lives in job/cw-overtime.sh (also the one-off
# backfill via cw-overtime-submit.sh); it takes this run's env. Never fatal:
# the series keeps its per-scan reads if a group fails.
begin over-time
GEN="$GEN" WORK="$WORK/over-time" bash job/cw-overtime.sh || echo "WARN: over-time groups failed (the series keeps its per-scan reads)" >&2
phase over-time

# 4e. Delete superseded index generations' files (specs/storage-consolidation.md
# phase 1): `index-gc` above drops only their D1 rows, so every reindex used to
# leave its previous `cw-l2/<scan>/index/<gen>/` behind in GCS and R2. Over every
# scan (`-R`: the row sweep ran above, for this scan): a dir goes only when no
# `index_schema` row names it, its scan still has a pointed `path` generation,
# and its newest object is older than the 2-day grace (an in-flight reindex is
# never raced). The site reads generations only through their pointers, so this
# is invisible to it. Needs D1; R2 only with its env. Never fatal.
begin index-gc
if [ -n "${CLOUDFLARE_API_TOKEN:+set}" ] && [ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then  # `:+set`: xtrace must not print the token
  GC_TARGETS=(-F "gs://$DATA")
  if [ -n "${R2_ENDPOINT:+set}" ] && [ -n "${R2_BUCKET:-}" ]; then GC_TARGETS+=(-F r2); fi
  dt-cloud index-gc -R -m 2d "${GC_TARGETS[@]}" \
    || echo "WARN: index-gc -F failed (superseded generations stay until the next run)" >&2
fi
phase index-gc


# 4b. Warm the site's subtree + diff caches for this scan (the colo cache, plus
# the global KV tier once `CACHE_KV` is bound in site/wrangler.toml) so the
# first viewer gets hits instead of a multi-second compute: the home page's
# default requests — one subtree, the diff span chips and the previous-scan
# pair — at the common canvas widths (`dt-cloud warm-cache`, which reads the
# SITE_URL / SNAPSHOTS_SUBDIR exported above). Auth is the job's machine grant
# (`SITE_TOKEN` ← Secret Manager `cw-s3-job-grant`, scope `cw`, minted
# from /admin — specs/done/oidc-cutover-cw.md P5); the CF Access pair is the
# pre-cutover form, kept for a rollback. Skipped when neither is set. Never fatal.
begin warm-cache
if { [ -n "${SITE_TOKEN:+set}" ] || { [ -n "${CF_ACCESS_CLIENT_ID:+set}" ] && [ -n "${CF_ACCESS_CLIENT_SECRET:+set}" ]; }; } \
   && [ "${REPROC:-0}" != "1" ]; then  # `:+set`: xtrace must not print the tokens
  dt-cloud warm-cache -d "$SNAP_ID" -r "${R2_ARCHIVE:-gs://$DATA}/snapshots/cw" \
    || echo "WARN: cache warm-up failed for $SNAP_ID" >&2
else
  echo "no warm-cache auth (SITE_TOKEN, or CF_ACCESS_CLIENT_ID+SECRET) — skipping cache warm-up" >&2
fi
phase warm-cache

# 4c'. Replay the site's page loads uncached (`dt-cloud probe -c`, as the gcs
# job does): every response a 2xx/4xx, and one cold-latency record per scan
# under probes/cw/ (the time series of what a viewer waits past the warmed
# cache). Same token and gating as the warm-up; never fatal.
begin probe
if [ -n "${SITE_TOKEN:+set}" ] && [ "${REPROC:-0}" != "1" ]; then
  dt-cloud probe -c -u "$SITE_URL" -o "gs://$DATA/probes/cw/" \
    || echo "WARN: serving probe failed for $SNAP_ID (a 5xx or a dropped request; data is fine)" >&2
fi
phase probe

# 5. Converge the monthly Shape-C digest thread in Slack (specs/cw-slack-
# digest.md): the OP + one reply per UTC day (+ the day's provisional reply
# until its 12Z scan lands), into #cw-s3-usage. Only when SLACK_BOT_TOKEN +
# SLACK_CHANNEL are set — Shape C needs the Web API's per-message
# sender/avatar overrides. Settings: job/digest.yml (specs/digest-
# unification.md). `dt-cloud digest` also renders the plot into job/icons-cw/
# and `wrangler pages deploy`s it to the icons Pages project's `cw` branch, so
# it needs CLOUDFLARE_* + wrangler (both in this image). A failed digest never
# fails the scan.
begin digest
if [ -n "${SLACK_BOT_TOKEN:+set}" ] && [ -n "${SLACK_CHANNEL:-}" ]; then  # `:+set`: xtrace must not print the token
  dt-cloud digest -T cw -C job/digest.yml -r "gs://$DATA/snapshots/cw" \
    || echo "WARN: usage-digest step failed" >&2
else
  echo "no Slack bot transport (SLACK_BOT_TOKEN+SLACK_CHANNEL) — skipping usage digest" >&2
fi
phase digest

echo "CW-SCAN-JOB-DONE $SNAP_ID -> gs://$DATA/snapshots/cw/$SNAP_ID"
