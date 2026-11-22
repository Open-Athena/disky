# Sourced by `/admin/slack`'s digest re-apply task (`DIGEST_REAPPLY.env_script`, site/functions/api/digest-reapply.ts):
# the digest step's environment (`job/profiles/hf.yml`) without the scan head. The thread state and snapshots are on
# R2 (`r2://oa-hf-usage-index/…`), read through blobfs with R2's own credentials profile (`scan_steps.r2_profile`).
# The task carries only `DIGEST_REAPPLY.secrets` (hf-usage-* in Secret Manager) and `R2_BUCKET`.
# xtrace off for the block: `${!n}` would print the values.
{ set +x; } 2>/dev/null
# Secret Manager values can carry a trailing newline (`echo … | --data-file=-`): strip them before anything reads them.
for n in SITE_TOKEN SLACK_BOT_TOKEN CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID R2_ENDPOINT R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY; do
  if [ -n "${!n+set}" ]; then
    v=${!n}; v=${v#"${v%%[![:space:]]*}"}; v=${v%"${v##*[![:space:]]}"}
    export "$n=$v"
  fi
done
unset n v
export R2_BUCKET=${R2_BUCKET:-oa-hf-usage-index}
export SITE_URL=${SITE_URL:-https://hf.oa.dev}
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
echo "hf-env: image $(dt-cloud image-sha)" >&2
