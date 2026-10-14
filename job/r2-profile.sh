# Sourced (xtrace off): the R2 archive through blobfs (`r2://$R2_BUCKET/…`). `AWS_*` are CoreWeave's keys (the
# lister's), so R2 gets its own named profile, and `buckets.yml` (blobfs's per-bucket config, which `disk_tree.r2fs`
# reads too) gives the bucket its endpoint and that profile. Never `DISK_TREE_R2_ENDPOINT_URL`: it would override
# every bucket's endpoint, the CoreWeave ones included. Exports `R2_ARCHIVE=r2://$R2_BUCKET` when R2 is configured.
if [ -n "${R2_ENDPOINT:-}" ] && [ -n "${R2_BUCKET:-}" ] && [ -n "${R2_ACCESS_KEY_ID:-}" ]; then
  export AWS_SHARED_CREDENTIALS_FILE=${TMPDIR:-/tmp}/r2-credentials
  ( umask 077; printf '[r2]\naws_access_key_id = %s\naws_secret_access_key = %s\n' "$R2_ACCESS_KEY_ID" "$R2_SECRET_ACCESS_KEY" > "$AWS_SHARED_CREDENTIALS_FILE" )
  mkdir -p "$HOME/.config/disk-tree"
  printf 'buckets:\n  - uri: r2://%s\n    endpoint_url: %s\n    profile: r2\n' "$R2_BUCKET" "$R2_ENDPOINT" > "$HOME/.config/disk-tree/buckets.yml"
  export R2_ARCHIVE="r2://$R2_BUCKET"
fi
