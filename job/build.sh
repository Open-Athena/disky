#!/usr/bin/env bash
# Build + push the hf scan job's image via Cloud Build (no local Docker), from a staged context holding only what
# job/Dockerfile copies. The cron runs `:hf` (job/hf-batch-submit.sh), so a rebuild is how job / CLI / site-config
# changes reach the next scan.
#
# Every image is stamped with its exact `git rev-parse HEAD` and dirty state: OCI labels
# (`org.opencontainers.image.revision`, `disky.dirty`) and env (`DISKY_GIT_SHA`, `DISKY_GIT_DIRTY`), which
# `dt-cloud image-sha [--expect SHA]` reads back. Run a worktree's own `job/build.sh`: calling another checkout's from
# inside a worktree is refused (it would image that other checkout).
#
# Env: PROJECT, IMAGE (e.g. a throwaway tag to test a build), ALLOW_DIRTY=1 (build a tree with uncommitted changes to
# tracked files; stamped dirty), ALLOW_OTHER_CHECKOUT=1 (build this script's checkout from another's cwd).
set -euo pipefail
caller=$PWD
here=$(cd "$(dirname "$0")" && pwd)
root=$(git -C "$here" rev-parse --show-toplevel)

if caller_root=$(git -C "$caller" rev-parse --show-toplevel 2>/dev/null) && [ "$caller_root" != "$root" ] && [ "${ALLOW_OTHER_CHECKOUT:-}" != 1 ]; then
  echo "refusing: cwd is in checkout $caller_root, but this build.sh images $root" >&2
  echo "  run $caller_root/job/build.sh to image your checkout (ALLOW_OTHER_CHECKOUT=1 to build $root anyway)" >&2
  exit 1
fi

PROJECT=${PROJECT:-oa-internal-450019}
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/hf-usage/scan:hf}

sha=$(git -C "$root" rev-parse HEAD)
dirty_paths=$(git -C "$root" status --porcelain --untracked-files=no)
dirty=0
if [ -n "$dirty_paths" ]; then dirty=1; fi
echo "building checkout $root: sha $sha, dirty $dirty" >&2
if [ "$dirty" = 1 ]; then
  echo "$dirty_paths" | sed 's/^/  /' >&2
  if [ "${ALLOW_DIRTY:-}" != 1 ]; then
    echo "refusing to build a dirty tree: commit (or ALLOW_DIRTY=1 to build it stamped dirty)" >&2
    exit 1
  fi
fi

ctx=$(mktemp -d)
trap 'rm -rf "$ctx"' EXIT
mkdir -p "$ctx/cloud" "$ctx/job" "$ctx/site"
cp -R "$root/pyproject.toml" "$root/README.md" "$root/uv.lock" "$root/src" "$ctx/"
cp -R "$root/cloud/pyproject.toml" "$root/cloud/src" "$ctx/cloud/"
cp "$root/site/wrangler.toml" "$ctx/site/"
cp "$here/Dockerfile" "$here/cloudbuild.yaml" "$here/hf-scan-runs.json" "$here/digest.yml" "$ctx/job/"
cp -R "$here/icons-hf" "$here/nf" "$here/profiles" "$ctx/job/"
find "$ctx" -name __pycache__ -type d -prune -exec rm -rf {} +

echo "building $IMAGE (Cloud Build, project $PROJECT)" >&2
gcloud builds submit --project "$PROJECT" --config "$ctx/job/cloudbuild.yaml" --substitutions "_IMAGE=$IMAGE,_GIT_SHA=$sha,_GIT_DIRTY=$dirty" "$ctx"
