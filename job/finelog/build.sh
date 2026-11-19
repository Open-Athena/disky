#!/usr/bin/env bash
# Build the `access-jobs` image (Marin's Finelog client, pinned in `job/finelog/uv.lock`) from this checkout's commit:
# `dt-cloud image build` over `job/finelog/build.json`. A candidate tag (`<sha12>`), nothing live moves; then
#   job/finelog/build.sh promote TAG [-x sha256:…]   # move `:latest` (what `options.access-jobs.batch.image` names)
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
if [ "${1:-}" = promote ]; then
  shift
  exec dt-cloud image promote -c "$here/build.json" "$@"
fi
exec dt-cloud image build -c "$here/build.json" "$@"
