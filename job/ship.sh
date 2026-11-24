#!/usr/bin/env bash
# Ship a commit to hf: its CI green → a candidate image built from a pinned worktree at that commit → no scan running →
# promote (`:hf`, the submitter and the events service re-pointed) → the prod site (`scripts/prod-push hf`) → a manual
# scan, the canary of the new image (hf scans after every promote).
#
#   DT_TRIGGER_BY=<who> job/ship.sh SHA    # the whole lane; the manual scan records DT_TRIGGER_BY as who started it
#   job/ship.sh -n SHA                     # stop after the prod site (no manual scan)
#   job/ship.sh -c SHA                     # the candidate only (nothing live moves)
#   job/ship.sh -S SHA                     # skip the prod site (image + scan only)
#
# The image is built by the pinned worktree's own `job/build.sh` (`$SHIP_WT`, default `wt/ship-hf` beside the main
# clone; created detached if missing), never a working tree with edits in it. Logs go under `tmp/ship/<sha12>/`. The
# last line printed is one of: `CANDIDATE-OK` (-c), `SHIPPED` (-n), `SCAN-SUBMITTED <job>`; anything else failed, and
# the exit status is nonzero. Every step is gated on the one before it (`|| exit 1`), never on `set -e`. Promote waits
# up to `$SHIP_WAIT_MIN` (90) minutes for a running scan to end, then gives up (`MID-SCAN`) rather than move `:hf` under it.
set -uo pipefail

PROJECT=oa-internal-450019
REGION=us-central1
SUBMITTER=hf-usage-snapshot-submit
SCAN_FILTER='name~hf-usage-snapshot AND (status.state=RUNNING OR status.state=SCHEDULED OR status.state=QUEUED)'

mode=scan
site=1
while getopts cnS o; do
  case $o in
    c) mode=candidate ;;
    n) mode=ship ;;
    S) site= ;;
    *) echo "usage: $0 [-c|-n] [-S] SHA" >&2; exit 2 ;;
  esac
done
shift $((OPTIND - 1))
here=$(cd "$(dirname "$0")/.." && pwd)
cd "$here" || exit 2
S=$(git rev-parse --verify "${1:?usage: $0 [-c|-n] [-S] SHA}^{commit}") || exit 2
S12=${S:0:12}
if [ $mode = scan ] && [ -z "${DT_TRIGGER_BY:-}" ]; then
  echo "DT_TRIGGER_BY unset: who the manual scan records as starting it (or -n: no scan)" >&2; exit 2
fi
logs=tmp/ship/$S12
mkdir -p "$logs"
say() { echo "$(date -u +%H:%M:%SZ) $*"; }

# 1. CI on that commit
if ! GH_REPO=${GH_REPO:-Open-Athena/disky} ghws -w ci.yml "$S" > "$logs/ci.log" 2>&1; then
  say CI-FAIL; tail -40 "$logs/ci.log"; exit 1
fi
say CI-OK

# 2. The candidate, from a pinned worktree at that commit
wt=${SHIP_WT:-$(cd "$(git rev-parse --path-format=absolute --git-common-dir)/.." && pwd)/wt/ship-hf}
if [ ! -d "$wt" ]; then
  git worktree add --detach "$wt" "$S" > "$logs/worktree.log" 2>&1 || { say WORKTREE-FAIL; cat "$logs/worktree.log"; exit 1; }
fi
git -C "$wt" checkout -q --detach "$S" || { say CHECKOUT-FAIL; exit 1; }
(cd "$wt" && job/build.sh -m candidate > "$here/$logs/candidate.out" 2> "$here/$logs/candidate.err") \
  || { say BUILD-FAIL; tail -20 "$logs/candidate.err"; exit 1; }
cat "$logs/candidate.out"
img=$(head -1 "$logs/candidate.out"); dig=${img##*@}
[ -n "$img" ] && [ "$dig" != "$img" ] || { say "BUILD-FAIL (no <repository>@<digest> in its output)"; exit 1; }
[ $mode = candidate ] && { say CANDIDATE-OK; exit 0; }

# 3. Never promote under a running scan
live=
for i in $(seq 1 "${SHIP_WAIT_MIN:-90}"); do
  live=$(gcloud batch jobs list --location $REGION --project $PROJECT --filter="$SCAN_FILTER" --format='value(name.basename())')
  [ -z "$live" ] && break
  say "waiting on $live ($i)"; sleep 60
done
[ -n "$live" ] && { say "MID-SCAN ($live)"; exit 1; }

# 4. Promote
(cd "$wt" && job/build.sh promote "$S12" -x "$dig" > "$here/$logs/promote.log" 2>&1) \
  || { say PROMOTE-FAIL; tail -15 "$logs/promote.log"; exit 1; }
say PROMOTED

# 5. The prod site
if [ -n "$site" ]; then
  scripts/prod-push hf "$S" || { say "PROD-PUSH-FAIL ($logs/prod-push.log)"; exit 1; }
fi
[ $mode = ship ] && { say SHIPPED; exit 0; }

# 6. The manual scan, through the cron's own submitter
gcloud run jobs execute $SUBMITTER --region $REGION --project $PROJECT --wait \
  --update-env-vars "DT_TRIGGER=manual,DT_TRIGGER_BY=$DT_TRIGGER_BY" > "$logs/scan-submit.log" 2>&1 \
  || { say SCAN-SUBMIT-FAIL; tail -5 "$logs/scan-submit.log"; exit 1; }
# The newest live scan (gcloud's `--sort-by` with `--limit` lists nothing here, so sort locally)
job=$(gcloud batch jobs list --location $REGION --project $PROJECT --filter="$SCAN_FILTER" --format='value(createTime,name.basename())' | sort | tail -1 | cut -f2)
say "SCAN-SUBMITTED ${job:-(job not listed yet)}"
