#!/usr/bin/env bash
# Build + push the hf scan job's image via Cloud Build (no local Docker), from a staged context holding only what
# job/Dockerfile copies. The cron runs `:hf` (job/hf-batch-submit.sh), so a rebuild is how job / CLI / site-config
# changes reach the next scan.
#
# Env: PROJECT, IMAGE (e.g. a throwaway tag to test a build).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/.." && pwd)

PROJECT=${PROJECT:-oa-internal-450019}
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/hf-usage/scan:hf}

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
gcloud builds submit --project "$PROJECT" --config "$ctx/job/cloudbuild.yaml" --substitutions "_IMAGE=$IMAGE" "$ctx"
