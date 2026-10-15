#!/usr/bin/env bash
# The hf.oa.dev scan job (GCP Batch, daily): the Hugging Face org `open-athena`'s storage — its Storage Buckets and
# its model, dataset and space repos — as one store, served from R2. Every step is a shared CLI:
#   1. bulk-list   `hf://open-athena` → layer-1 listing shards (`find/bulk_hf.py`; HF_TOKEN sees private repos)
#   2. import      shards → the canonical layer-2 (`-p storage_class_id`: public 1, private 5, unbilled git 6)
#   3. index-write layer-2 → the path store (`path` + `bysize` sorts, age pyramid, search sidecars)
#   4. path-index  the listing → the scan's snapshot JSONs (`age.json`, `meta.json`)
#   5. upload      tiers, snapshot, layer-2 and the listing's `_SUCCESS.json` to the R2 index bucket
#   6. static-names add (`build` on the first run): the scan into the static name index, on R2
#   7. index-sync  the tiers' footers → D1 (reading them back from R2): the scan is listed
#   8. interval-store add (`build` on the first run): the scan into the change-interval path store, on R2
#   9. index-gc    D1 rows + R2 dirs of superseded generations
#
# R2 only: no GCS archive (no archival copy in two clouds). The VM's own disk is the scratch.
#
# Why the path store comes from `index-write` (the layer-2) and not `path-index -P` (the listing, as the r2 demo's
# daily ingest does): `path-index` re-aggregates the listing with the GCS storage-class pivots (2–4) only, so the
# public / private / git split (classes 1, 5, 6) would be lost; `index-write` carries the layer-2's pivots as they
# are. `path-index` still writes the snapshot JSONs (no `-P`). The import is `-e duckdb`: the HF lister writes its
# shards in API order, not key order, and the stream engine wants sorted listings.
#
# Sizing (measure and adjust): the listing is bound by the Hub's rate limit (1000 requests / 5 min; the org is
# ~2,000 requests at the default per-file commit dates, ~10 min), the rest is seconds to minutes on a few million
# rows at most. e2-standard-4, on demand (`job/hf-batch-submit.sh`).
#
# Env (`job/hf-batch-submit.sh` sets them on Batch; every one has a default or is a secret):
#   HF_URI (default hf://open-athena; the namespace is the store's one bucket), HF_THREADS (requests in flight),
#   SNAP_ID (default the UTC start minute, `YYYY-MM-DDTHHMM`, as gcs/cw), STORE_ROOT (default r2://$R2_BUCKET; a local dir
#   for a dry run), WORK_DIR, MEM (DuckDB memory limit). Secrets: HF_TOKEN, CLOUDFLARE_API_TOKEN (D1 edit),
#   CLOUDFLARE_ACCOUNT_ID, R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, SLACK_BOT_TOKEN (the HF Usage Bot: the
#   digest to SLACK_CHANNEL, failure alerts to SLACK_ALERT_CHANNEL), SITE_TOKEN (the site read grant: cache warm-up, the
#   serving probe, the digest's plot), CF_PAGES_TOKEN (Pages edit: the digest's plot upload).
# Deployment config comes from `site/wrangler.toml` (baked into the image): the D1 (`database_id` /
# `database_name`), and the static-names / interval-store generations (`STATIC_GEN` / `INTERVAL_STORE_GEN`), so the
# job writes exactly the generations the site reads.
set -euxo pipefail

# Secrets arrive from Secret Manager verbatim, and a version made with `echo … | --data-file=-` carries a trailing
# newline. Strip surrounding whitespace off each before anything reads it; xtrace off (`${!n}` would print them).
{ set +x; } 2>/dev/null
for n in HF_TOKEN CLOUDFLARE_API_TOKEN CF_PAGES_TOKEN CLOUDFLARE_ACCOUNT_ID R2_ENDPOINT R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY \
         SLACK_BOT_TOKEN SITE_TOKEN; do
  if [ -n "${!n+set}" ]; then
    v=${!n}; v=${v#"${v%%[![:space:]]*}"}; v=${v%"${v##*[![:space:]]}"}
    export "$n=$v"
  fi
done
unset n v
# R2 through blobfs / s3fs: the job's only S3-compatible store is R2, so its keys are the ambient AWS ones and its
# endpoint the global one (no other bucket's endpoint to override).
if [ -n "${R2_ENDPOINT:-}" ]; then
  export DISK_TREE_R2_ENDPOINT_URL=$R2_ENDPOINT AWS_ACCESS_KEY_ID=${R2_ACCESS_KEY_ID:-} AWS_SECRET_ACCESS_KEY=${R2_SECRET_ACCESS_KEY:-}
  export AWS_DEFAULT_REGION=auto
fi
set -x

cd "$(dirname "$0")/.."   # /app in the image (job/ + site/wrangler.toml), the repo root in a checkout
WRANGLER=${WRANGLER_TOML:-site/wrangler.toml}
wvar() { sed -nE "s/^$1 *= *\"([^\"]*)\".*/\\1/p" "$WRANGLER" | head -1; }

URI=${HF_URI:-hf://open-athena}
NS=${URI#hf://}; NS=${NS%%/*}             # the namespace: the listing's (and the store's) one bucket
R2_BUCKET=${R2_BUCKET:-oa-hf-usage-index}
STORE_ROOT=${STORE_ROOT:-r2://$R2_BUCKET}
SNAP_ID=${SNAP_ID:-$(date -u +%Y-%m-%dT%H%M)}   # as gcs/cw: a re-run is a new scan, never a second answer for one
export SNAP_ID
DATE=${SNAP_ID%%T*}
HM=${SNAP_ID#"$DATE"}; HM=${HM#T}
if [ -n "$HM" ]; then SCAN_TIME="${DATE}T${HM:0:2}:${HM:2:2}:00+00:00"; else SCAN_TIME="${DATE}T00:00:00+00:00"; fi
GEN=${GEN:-$(date -u +%Y%m%dT%H%M%SZ)}   # this run's index generation, stamped once
MEM=${MEM:-8GB}
NPROC=$(nproc 2>/dev/null || sysctl -n hw.ncpu)
SUB=hf                                    # snapshots/hf/ (`SNAPSHOTS_SUBDIR` in site/wrangler.toml)
INDEX_KEY="listing/$SNAP_ID/index/$GEN"
LAYOUT='listing/{id}/index/{gen}/path-index.parquet'
export D1_DB_ID=${D1_DB_ID:-$(wvar database_id)}
export D1_DB_NAME=${D1_DB_NAME:-$(wvar database_name)}
export SNAPSHOTS_SUBDIR=$SUB SITE_URL=${SITE_URL:-https://hf.oa.dev}
STATIC_GEN=${STATIC_GEN:-$(wvar STATIC_GEN)}
IV_PIN=${INTERVAL_STORE_GEN:-$(wvar INTERVAL_STORE_GEN)}   # empty: follow `interval-store/CURRENT.json`

# Scan-run record + failure alerts (specs/scan-job-wrapper.md): each phase's PHASE line, the run in D1 (`scan_runs`,
# shown at /scans) under Batch (BATCH_JOB_UID is the run id), and one alert to SLACK_ALERT_CHANNEL on a failure,
# redacted of the profile's `secrets`. Never fails the job; xtrace is off around it.
export SCAN_RUNS_PROFILE=${SCAN_RUNS_PROFILE:-$PWD/job/hf-scan-runs.json}
export DT_JOB_START=${DT_JOB_START:-$(date +%s)}
sr() { { set +x; } 2>/dev/null; local rc=0; dt-cloud scan-run "$@" || rc=$?; set -x; return "$rc"; }
begin() { sr phase-begin "$1"; }
phase() { sr phase-end "$1" ${2:+-q "$2"}; }
# ERR names the failed command for the record and the alert. The backfill matches this function's xtrace line
# (`error_marker`), so its name and argument order are a contract.
scan_err() { { set +x; } 2>/dev/null; SR_ERR="exit $1: $3 (hf-run.sh:$2)"; }
on_exit() { local rc=$?; sr finish -x "$rc" ${SR_ERR:+-e "$SR_ERR"}; exit "$rc"; }
SR_ERR=""
trap 'scan_err $? $LINENO "$BASH_COMMAND"' ERR
trap on_exit EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
sr start -B -g "$GEN" "$SNAP_ID"

WORK=${WORK_DIR:-/tmp/hf-$SNAP_ID}
mkdir -p "$WORK"
exists() {  # exists KEY: whether the store holds KEY
  python -c 'import sys; from dt_cloud.blobstore import Store; sys.exit(0 if Store(sys.argv[1]).exists(sys.argv[2]) else 1)' "$STORE_ROOT" "$1"
}

# 1. The listing. `-x clear`: a retried task starts from a clean dir. The marker (`_SUCCESS.json`) carries the
# reconciliation against billing: `hf.expected` = Σ bucket `size` + Σ repo `usedStorage`.
begin list
disk-tree bulk-list "$URI" -o "$WORK/listing" -w "${HF_THREADS:-3}" -x clear
phase list

# 2. Layer-2, with the billing-class pivots (`sum_storage_class_id_{1,5,6}`) and the size-weighted mean mtime.
begin import
env DISK_TREE_ROOT="$WORK/l2" \
  disk-tree import -e duckdb -M "$MEM" -n "$NPROC" -m -p storage_class_id \
    -s hf -t "$SCAN_TIME" -l "$WORK/listing/shard-*.parquet" -b "$NS"
L2=$(ls "$WORK/l2"/scans/*.parquet | head -1)
phase import

# 3. The path store, bucket-prefixed (`open-athena/models/…`), spill outside the upload dir.
begin index-write
dt-cloud index-write -S -m "$MEM" -t "$NPROC" -T "$WORK/duckdb-tmp" -o "$WORK/index" "$NS=$L2"
phase index-write

# 4. The snapshot JSONs (`meta.json` carries the listing's `started`, which index-sync records on the pointer).
begin snapshot
dt-cloud path-index -d "$SNAP_ID" -l "$WORK/listing/*.parquet" -o "$WORK/snap"
phase snapshot

# 5. Upload: data first, the completeness markers (`meta.json`) last. Only the tiers' parquets go up (as cw's job
# copies them); the layer-2 sits beside its index dir (`listing/<scan>/<ns>.parquet`, cw's `cw-l2/` shape under the
# base's `listing/` prefix), with the listing's marker as `<ns>.listing.json`.
begin upload
python - "$STORE_ROOT" "$WORK" "$SNAP_ID" "$INDEX_KEY" "$NS" "$L2" "$SUB" <<'PY'
import sys
from pathlib import Path
from dt_cloud.blobstore import Store
root, work, scan, index_key, ns, l2, sub = sys.argv[1:]
s, w = Store(root), Path(work)
tiers = sorted(p for p in (w / "index").iterdir() if p.suffix == ".parquet")
for p in tiers:
    s.put(p, f"{index_key}/{p.name}")
s.put(Path(l2), f"listing/{scan}/{ns}.parquet")
s.put(w / "listing" / "_SUCCESS.json", f"listing/{scan}/{ns}.listing.json")
snap = s.put_tree(w / "snap", f"snapshots/{sub}/{scan}", last=("meta.json",))
print(f"upload: {len(tiers)} tiers → {root}/{index_key}; layer-2; {len(snap)} snapshot files", file=sys.stderr)
PY
phase upload

# 6. The static name index (the map's `?f=` and /names), one machine on the R2 store: `add` appends every scan after
# the generation's newest (a date it already holds is not re-appended); the first run `build`s the generation the
# site names. Before index-sync, so a listed scan is searchable. Non-fatal: its search reads "not indexed" until a
# later run's `add` catches up.
if [ -n "$STATIC_GEN" ]; then
  sn=(-g "$STATIC_GEN" -L "$LAYOUT" -s "$STORE_ROOT" -o "$STORE_ROOT" -w "$WORK/static-names" -p "$NPROC" -M "$MEM")
  if exists "static-names/$STATIC_GEN/scans.json"; then sn=(add "${sn[@]}"); else sn=(build "${sn[@]}" -x 16,8); fi
  sr phase static-names -W -T "${STATIC_NAMES_TIMEOUT:-60m}" -m "the scan is listed; its name search says not indexed until a later run appends it" \
    -- dt-cloud static-names "${sn[@]}"
fi

# 7. Footers → D1, read back from the uploaded generation: the scan is listed (the pointer flips last).
if [ -n "${CLOUDFLARE_API_TOKEN:+set}" ] && [ -n "${CLOUDFLARE_ACCOUNT_ID:+set}" ]; then  # `:+set`: xtrace must not print them
  sr phase index-sync -m "D1 pointers not advanced; the site keeps serving the previous scan" \
    -- dt-cloud index-sync -d "$STORE_ROOT/$INDEX_KEY" -g "$GEN" -k "$INDEX_KEY" -m "$WORK/snap/meta.json" "$SNAP_ID"
else
  echo "WARN: no CLOUDFLARE_API_TOKEN/ACCOUNT_ID — tiers uploaded but not synced (the scan stays unlisted)" >&2
fi

# 8. The interval store (every scan's tree as versioned intervals; the site reads it under `PATH_STORE`), one machine
# on the R2 store, reading the scans' `path` sorts from it. Non-fatal: a scan it doesn't hold reads per-scan.
# A pinned generation (INTERVAL_STORE_GEN in site/wrangler.toml) is appended to without compacting: a compacted one
# leaves R2 after the grace, and the pinned site would lose it. Unpinned (hf's default), the store's current one,
# compacted every 2^5 scans (`add -C`, default 5) with CURRENT.json, which the site follows, moved to the new one.
iv=(-L "$LAYOUT" -s "$STORE_ROOT" -o "$STORE_ROOT" -w "$WORK/interval-store" -p "$NPROC" -M "$MEM")
IV_CUR=$(dt-cloud interval-store current -o "$STORE_ROOT")   # CURRENT.json's, else the only one; empty: none yet
if [ -n "$IV_PIN" ]; then iv=(add -g "$IV_PIN" -C none "${iv[@]}")
elif [ -n "$IV_CUR" ]; then iv=(add "${iv[@]}")
else iv=(build -g "${DATE}hf" "${iv[@]}"); fi
sr phase interval-store -W -T "${INTERVAL_STORE_TIMEOUT:-60m}" -m "the scan reads per-scan until a later run appends it" \
  -- dt-cloud interval-store "${iv[@]}"

# 9. Superseded generations (a same-day re-run's previous one): their D1 rows (every scan's; the store is small),
# then their R2 dirs older than the grace.
if [ -n "${CLOUDFLARE_API_TOKEN:+set}" ] && [ -n "${CLOUDFLARE_ACCOUNT_ID:+set}" ]; then
  sr phase index-gc -W -m "superseded generations stay until the next run" \
    -- dt-cloud index-gc -m 2d -F "$STORE_ROOT"
fi

# 10. Warm the site's subtree + diff caches for this scan (the home page's default requests at the common widths; the
# colo cache + CACHE_KV), so the first viewer gets hits. 11. Replay the page loads uncached (`probe -c`): every
# response a 2xx/4xx, one cold-latency record per scan under `probes/hf/`. Both authenticate with the job's read
# grant (`SITE_TOKEN`, minted from /admin; specs/hf-deployment.md) and are skipped without it. Never fatal.
if [ -n "${SITE_TOKEN:+set}" ]; then  # `:+set`: xtrace must not print the token
  sr phase warm-cache -W -M -m "the first viewers of this scan compute their views cold" \
    -- dt-cloud warm-cache -d "$SNAP_ID" -r "$STORE_ROOT/snapshots/$SUB"
  sr phase probe -W -M -m "a page load 5xx'd or dropped (the data is fine); see the probe record" \
    -- dt-cloud probe -c -u "$SITE_URL" -o "$STORE_ROOT/probes/$SUB/"
else
  echo "no SITE_TOKEN: cache warm-up and the serving probe skipped" >&2
fi

# 12. Converge the month's Slack digest thread in #hf-usage (`job/digest.yml`: the `cw` template, one reply per UTC
# day, the headline as the sender name, % of the public-storage allowance): the OP (month-to-date, per-week bullets,
# the site-drawn plot, `POST /api/digest-image` with SITE_TOKEN, uploaded to the icons project's `plot` branch with
# the Pages token) edited in place, plus the day's reply. State beside the snapshots (`digest/hf/…`), so a re-run is a
# no-op. Never fatal; skipped without the bot, the channel, the site grant or the Pages token.
if [ -n "${SLACK_BOT_TOKEN:+set}" ] && [ -n "${SLACK_CHANNEL:-}" ] && [ -n "${SITE_TOKEN:+set}" ] && [ -n "${CF_PAGES_TOKEN:+set}" ]; then
  # wrangler authenticates with CLOUDFLARE_API_TOKEN: the Pages token for this one command (single-quoted, so xtrace
  # prints the variable's name, never its value); the D1 token stays the scan-run records'.
  sr phase digest -W -M -m "today's #hf-usage digest reply didn't post (the scan is fine)" \
    -- bash -c 'CLOUDFLARE_API_TOKEN=$CF_PAGES_TOKEN exec dt-cloud digest -T cw -C job/digest.yml -r "$1"' _ "$STORE_ROOT/snapshots/$SUB"
else
  echo "no SLACK_BOT_TOKEN + SLACK_CHANNEL + SITE_TOKEN + CF_PAGES_TOKEN: the Slack digest skipped" >&2
fi

echo "HF-SCAN-JOB-DONE $SNAP_ID -> $STORE_ROOT/$INDEX_KEY"
