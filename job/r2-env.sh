# Sourced by `/admin/slack`'s digest re-apply task (`DIGEST_REAPPLY.env_script`, site/functions/api/digest-reapply.ts):
# the digest step's environment (`job/profiles/r2.yml`) without the scan head. The thread state and snapshots are on
# R2 (`r2://oa-r2-usage-index/…`), read through blobfs with R2's own credentials profile (`scan_steps.r2_profile`).
# That writes the job's `buckets.yml` registry, so it runs on a Batch task; to source this anywhere else, point
# `DISKY_ROOT` at a scratch dir first (`r2_profile` refuses to replace a machine's own `~/.config/disk-tree`).
# xtrace off for the block: `${!n}` would print the values.
{ set +x; } 2>/dev/null
# Secret Manager values can carry a trailing newline (`echo … | --data-file=-`): strip them before anything reads them.
for n in SITE_TOKEN SLACK_BOT_TOKEN CLOUDFLARE_API_TOKEN R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY; do
  if [ -n "${!n+set}" ]; then
    v=${!n}; v=${v#"${v%%[![:space:]]*}"}; v=${v%"${v##*[![:space:]]}"}
    export "$n=$v"
  fi
done
unset n v
export CLOUDFLARE_ACCOUNT_ID=${CLOUDFLARE_ACCOUNT_ID:-74981a43be0de7712369306c7b19133d}
export R2_ENDPOINT=https://$CLOUDFLARE_ACCOUNT_ID.r2.cloudflarestorage.com
export R2_BUCKET=${R2_BUCKET:-oa-r2-usage-index}
export AWS_DEFAULT_REGION=auto AWS_EC2_METADATA_DISABLED=true
eval "$(python - <<'EOF'
from os import environ
from shlex import quote

from dt_cloud.scan_steps import r2_profile

env = dict(environ)
r2_profile(env)
for k in ("AWS_SHARED_CREDENTIALS_FILE", "R2_ARCHIVE"):
    print(f"export {k}={quote(env[k])}")
EOF
)"
echo "r2-env: image $(dt-cloud image-sha)" >&2
