#!/usr/bin/env bash
# Ship a commit's scan job image: its CI green → a candidate built from a pinned worktree at that commit → a smoke run
# on Batch → no scan running → promote (`:latest`, the submitter and alert relay re-pointed) → a manual scan, so the
# 07:00 scheduled scan is never the first run of a new image.
#
#   DT_TRIGGER_BY=<who> job/ship.sh SHA    # the whole lane; the manual scan records DT_TRIGGER_BY as who started it
#   job/ship.sh -n SHA                     # stop after promote (no manual scan)
#   job/ship.sh -c SHA                     # candidate + smoke only (nothing live moves)
#
# The image is built by the pinned worktree's own `job/build.sh` (`$SHIP_WT`, default `wt/ship-gcs` beside the main
# clone; created detached if missing), never a working tree with edits in it. Logs go under `tmp/ship/<sha12>/`. The
# last line printed is one of: `CANDIDATE-OK` (-c), `PROMOTED` (-n), `SCAN-SUBMITTED <job>`; anything else failed, and
# the exit status is nonzero. Promote waits up to `$SHIP_WAIT_MIN` (90) minutes for a running scan head to end, then
# gives up (`MID-SCAN`) rather than move `:latest` under it.
set -uo pipefail

PROJECT=oa-internal-450019
HEAD_REGION=us-central1    # the scan head and its submitter
SMOKE_REGION=us-east1      # beside the data bucket
SUBMITTER=gcs-usage-snapshot-submit
HEAD_FILTER='name~gcs-usage-snapshot AND (status.state=RUNNING OR status.state=SCHEDULED OR status.state=QUEUED)'

mode=scan
while getopts cn o; do
  case $o in
    c) mode=candidate ;;
    n) mode=promote ;;
    *) echo "usage: $0 [-c|-n] SHA" >&2; exit 2 ;;
  esac
done
shift $((OPTIND - 1))
here=$(cd "$(dirname "$0")/.." && pwd)
cd "$here"
S=$(git rev-parse --verify "${1:?usage: $0 [-c|-n] SHA}^{commit}") || exit 2
S12=${S:0:12}
if [ $mode = scan ] && [ -z "${DT_TRIGGER_BY:-}" ]; then
  echo "DT_TRIGGER_BY unset: who the manual scan records as starting it (or -n: no scan)" >&2; exit 2
fi
logs=tmp/ship/$S12
mkdir -p "$logs"
say() { echo "$(date -u +%H:%M:%SZ) $*"; }

# 1. CI on that commit
if ! GH_REPO=${GH_REPO:-Open-Athena/disky} ghws "$S" > "$logs/ci.log" 2>&1; then
  say CI-FAIL; tail -40 "$logs/ci.log"; exit 1
fi
say CI-OK

# 2. The candidate, from a pinned worktree at that commit
wt=${SHIP_WT:-$(cd "$(git rev-parse --path-format=absolute --git-common-dir)/.." && pwd)/wt/ship-gcs}
if [ ! -d "$wt" ]; then
  git worktree add --detach "$wt" "$S" > "$logs/worktree.log" 2>&1 || { say WORKTREE-FAIL; cat "$logs/worktree.log"; exit 1; }
fi
git -C "$wt" checkout -q --detach "$S" && (cd "$wt" && pnpm install --frozen-lockfile > "$here/$logs/pnpm-install.log" 2>&1) \
  || { say CHECKOUT-OR-INSTALL-FAIL; exit 1; }
(cd "$wt" && job/build.sh -m candidate > "$here/$logs/candidate.out" 2> "$here/$logs/candidate.err") \
  || { say BUILD-FAIL; tail -20 "$logs/candidate.err"; exit 1; }
cat "$logs/candidate.out"
img=$(grep "^$SMOKE_REGION-" "$logs/candidate.out"); dig=${img##*@}
[ -n "$img" ] && [ -n "$dig" ] || { say "BUILD-FAIL (no $SMOKE_REGION image in its output)"; exit 1; }

# 3. Smoke: the image's contents and its stamp, on Batch
smoke=image-smoke-$S12-$(date -u +%H%M)
"$here/job/smoke-spec.py" "$img" "$S" > "$logs/smoke-spec.json" \
  && DISKY_LABELS=app=disky,deployment=gcs dt-cloud batch submit -c smoke -L $SMOKE_REGION -p $PROJECT "$smoke" "$logs/smoke-spec.json" > /dev/null \
  || { say SMOKE-SUBMIT-FAIL; exit 1; }
BATCH_REGION=$SMOKE_REGION GCP_PROJECT=$PROJECT dt-cloud job watch -L -i 20 "$smoke" > "$logs/smoke-watch.log" 2>&1
st=$(gcloud batch jobs describe "$smoke" --location $SMOKE_REGION --project $PROJECT --format='value(status.state)')
say "smoke $smoke: $st"
[ "$st" = SUCCEEDED ] || exit 1
[ $mode = candidate ] && { say CANDIDATE-OK; exit 0; }

# 4. Never promote under a running scan
live=
for i in $(seq 1 "${SHIP_WAIT_MIN:-90}"); do
  live=$(gcloud batch jobs list --location $HEAD_REGION --project $PROJECT --filter="$HEAD_FILTER" --format='value(name.basename())')
  [ -z "$live" ] && break
  say "waiting on $live ($i)"; sleep 60
done
[ -n "$live" ] && { say "MID-SCAN ($live)"; exit 1; }

# 5. Promote
(cd "$wt" && job/build.sh promote "$S12" -x "$dig" > "$here/$logs/promote.log" 2>&1) \
  || { say PROMOTE-FAIL; tail -15 "$logs/promote.log"; exit 1; }
say PROMOTED
[ $mode = promote ] && exit 0

# 6. The manual scan, through the cron's own submitter
gcloud run jobs execute $SUBMITTER --region $HEAD_REGION --project $PROJECT --wait \
  --update-env-vars "DT_TRIGGER=manual,DT_TRIGGER_BY=$DT_TRIGGER_BY" > "$logs/scan-submit.log" 2>&1 \
  || { say SCAN-SUBMIT-FAIL; tail -5 "$logs/scan-submit.log"; exit 1; }
# The newest live head (gcloud's `--sort-by` with `--limit` lists nothing here, so sort locally)
job=$(gcloud batch jobs list --location $HEAD_REGION --project $PROJECT --filter="$HEAD_FILTER" --format='value(createTime,name.basename())' | sort | tail -1 | cut -f2)
say "SCAN-SUBMITTED ${job:-(job not listed yet)}"
