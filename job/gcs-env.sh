# Sourced by a job the site starts outside the scan (`/admin/slack`'s digest re-apply: `DIGEST_REAPPLY.env_script`):
# the scan steps' environment, which a scan gets from `job/profiles/gcs.yml` (`env:`) and the re-apply job doesn't.
# Secrets arrive from Secret Manager verbatim, and a version created with `echo … | --data-file=-` carries a trailing
# newline: strip surrounding whitespace off each before anything reads it (xtrace off: `${!n}` would print the values).
{ set +x; } 2>/dev/null
for n in SITE_TOKEN SLACK_BOT_TOKEN CLOUDFLARE_API_TOKEN R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY; do
  if [ -n "${!n+set}" ]; then
    v=${!n}; v=${v#"${v%%[![:space:]]*}"}; v=${v%"${v##*[![:space:]]}"}
    export "$n=$v"
  fi
done
unset n v
echo "gcs-env: image $(dt-cloud image-sha)" >&2

# As `job/profiles/gcs.yml`'s `env:` (the digest's channel and the stores it reads and writes)
export SITE_URL=${SITE_URL:-https://gcs.oa.dev}
export SLACK_CHANNEL=${SLACK_CHANNEL:-C0BNWAASXFW}   # #gcs-usage
export CLOUDFLARE_ACCOUNT_ID=${CLOUDFLARE_ACCOUNT_ID:-74981a43be0de7712369306c7b19133d}
export R2_ENDPOINT=${R2_ENDPOINT:-https://74981a43be0de7712369306c7b19133d.r2.cloudflarestorage.com}
export D1_DB_ID=${D1_DB_ID:-e52398b7-5538-4bc4-83db-3355a1b5ef9a}
export D1_DB_NAME=${D1_DB_NAME:-oa-gcs-usage-auth}
