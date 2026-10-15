# hf.oa.dev: the Hugging Face deployment

Status: **applying** (2026-10-15, the `wt/hf` session). Done: steps 1–5, 6 except the Pages secrets and the OAuth client, 7, and the dev stack's CF side; the first scan (step 9) is running. Waiting: the Pages secrets (`hf-secrets -P -d`, needs `RESEND_API_KEY` from `$oa/.envrc`) and the OAuth client's Create, both Ryan's (Claude Code's safety check refuses them), then 8 and 10.

## What it is

A new OA deployment, modeled on cw-s3 (GCP Batch job → R2 + D1 → Cloudflare Pages), serving the Hugging Face org `open-athena` — its Storage Buckets and its model, dataset and space repos — as one store with one bucket (the namespace), at hf.oa.dev, to Open Athena staff.

| | name |
|---|---|
| GCP project / region | `oa-internal-450019` / `us-central1` (as cw-s3) |
| job service account | `hf-usage-job` |
| image | `us-central1-docker.pkg.dev/oa-internal-450019/hf-usage/scan:hf` (repo `hf-usage`) |
| cron | `hf-usage-snapshot`, daily 08:00 UTC, Batch on one `e2-standard-4`, on demand |
| secrets | `hf-usage-hf-token`, `hf-usage-cf-token`, `hf-usage-cf-account-id`, `hf-usage-r2-endpoint`, `hf-usage-r2-access-key-id`, `hf-usage-r2-secret-access-key`, `hf-usage-slack-bot-token` |
| Pulumi stacks | `hf` in `infra/gcp` (project `gcs-usage-gcp`) and `infra/cf` (project `gcs-usage-cf`), backend `gs://oa-pulumi` |
| Pages project / domains | `oa-hf-usage` / `hf.oa.dev`, dev stack `dev.hf.oa.dev` (branch alias `dev`; OA Cloudflare account, zone `oa.dev`) |
| D1 / KV / R2 | `oa-hf-usage-db` (cw lineage) / `oa-hf-usage-cache` / `oa-hf-usage-index` |
| cost labels | `app=disky,deployment=hf` (`$DISKY_LABELS`) |

## The daily job

`job/hf-run.sh` under `dt-cloud scan-run supervise` (the image's entrypoint and the Batch command), with `specs/scan-job-wrapper.md`'s ERR / EXIT / TERM / INT traps, `scan-run start` / phases / `finish`, and the profile `job/hf-scan-runs.json` (alerts to #gcs-usage-alerts, the secrets redacted). Every step is a shared CLI:

1. `disk-tree bulk-list hf://open-athena` (Hub read token `HF_TOKEN`; per-file commit dates; 3 requests in flight). Rate-bound: ~2,000 requests at 1000 / 5 min, ~10 min.
2. `disk-tree import -e duckdb -m -p storage_class_id -s hf -b open-athena`: the layer-2 with `sum_storage_class_id_{1,5,6}` (public, private, unbilled git).
3. `dt-cloud index-write -S`: the path store (`path` + `bysize` sorts, age pyramid, search sidecars) from the layer-2.
4. `dt-cloud path-index` (no `-P`): the snapshot JSONs (`age.json`, `meta.json` with the listing's `started`).
5. Upload to R2 (`dt_cloud.blobstore.Store`, markers last): `listing/<scan>/index/<gen>/*.parquet`, `listing/<scan>/open-athena.parquet` (layer-2), `listing/<scan>/open-athena.listing.json` (the lister's `_SUCCESS.json`: `hf.expected` = Σ bucket size + Σ repo `usedStorage`, the request count), `snapshots/hf/<scan>/`.
6. `dt-cloud static-names add` (`build -x 16,8` when the generation doesn't exist), on the R2 store. Non-fatal (`-W`).
7. `dt-cloud index-sync -d r2://oa-hf-usage-index/listing/<scan>/index/<gen>`: footers → D1, pointer last. Fatal: the scan isn't listed without it.
8. `dt-cloud interval-store add` (`build` the first time), on the R2 store. Non-fatal.
9. `dt-cloud index-gc -m 2d -F r2://oa-hf-usage-index`: superseded generations' D1 rows and R2 dirs. Non-fatal.

The static-names and interval-store generations, and the D1, are read from `site/wrangler.toml` (baked into the image), so the job always writes what the site reads.

Decisions, and where they differ from what was asked:
- **R2 only.** No GCS bucket at all, not even scratch: the VM's boot disk (100 GB pd-balanced) is the work dir.
- **The path store comes from `index-write`, not `path-index -P`.** `path-index` re-aggregates the listing and carries only the GCS tier pivots (2–4), so the public / private / git split would be lost; `index-write` cuts the layer-2 as it is. `path-index` still writes the snapshot JSONs. Its `meta.json` `class_bytes` folds classes 5 and 6 into 1 (a generic `viz.py` limitation; hidden here since `prices: false`).
- **`-e duckdb`, not `-e stream`.** The HF lister writes shards in API order, and the stream engine wants key-sorted listings.
- **Timestamped scan ids** (`YYYY-MM-DDTHHMM`, the run's start minute, as gcs/cw). A re-run is a new scan. Under date ids, a same-day re-run re-pointed the date's path store at a new listing while the append-only static index and interval store kept the first, so `ps=iv` and the per-scan read gave two answers for one scan (r2.rbw.sh, 2026-10-15). The first scan, `2026-10-15`, predates the switch and keeps its date id.
- **Layout under the base's `listing/` prefix** (cw's `cw-l2/<scan>/` shape), so the site needs no `STORE_PREFIXES`.

## Billing / quota

The org's billing page (read 2026-10-14) shows public storage 15.7 TB of 253 TB (including a 245 TB contract add-on), private 0.02 TB of 5.1 TB (including a 5 TB add-on), and egress 5.12 TB of 30 TB this month. The Hub has no API for these, so the allowances are declared values in `site/src/stores/hf.tsx` (`HF_ALLOWANCES`, with the source in its comment) and shown in the About panel. Bars or headroom on the map are a follow-up.

## Apply checklist

Run from the `hf` worktree (e.g. `wt/hf`). Its `.envrc` (untracked) needs `source_up` plus:

```bash
export DISKY_LABELS=app=disky,deployment=hf
export CLOUDFLARE_ACCOUNT_ID=<OA account id>     # the same one cw-s3 uses
export CF_ZONE_ID=<oa.dev zone id>               # cw-s3's `zoneId`
export UV_PROJECT_ENVIRONMENT=$PWD/infra/.venv   # for pulumi; never the repo's .venv
# CF_IAC_TOKEN, RESEND_API_KEY: from the root / OA .envrc
```

1. **Review + branch.** `git branch hf <this branch's tip>`; check the `[cloud]` commit is picked up to `cloud` separately.
2. **CF stack.**
   ```bash
   cd infra/cf
   ./run stack init hf --secrets-provider gcpkms://projects/oa-internal-450019/locations/global/keyRings/ops/cryptoKeys/pulumi
   git add Pulumi.hf.yaml && git commit -m '[hf] `cf/`: stack `hf` initialized'   # the added secretsprovider/encryptedkey lines
   ./run preview -s hf   # expect 10 creates: CfnDashboard (pages, cname, domain, d1, cache-kv, r2, r2-token) + hf-site-r2-reader + hf-job-d1
   ./run up -s hf
   ```
   The DNS / domain step is part of this `up`: a proxied CNAME `hf.oa.dev → oa-hf-usage.pages.dev` in the oa.dev zone, and the Pages custom domain (validated by Cloudflare, usually within minutes).
3. **Fill the binding ids** in `site/wrangler.toml`: `database_id` ← `./run stack output d1_database_id -s hf`, the `CACHE_KV` `id` ← `./run stack output cache_kv_id -s hf`. Commit (`[hf] site/wrangler.toml: the D1 + KV ids`). The image bakes this file, so do it before step 7.
4. **D1 migrations** (the cw lineage, from scratch):
   ```bash
   cd site && pnpm exec wrangler d1 migrations apply oa-hf-usage-db --remote
   pnpm exec wrangler d1 execute oa-hf-usage-db --remote --command "SELECT name FROM d1_migrations ORDER BY id"   # 20 rows, 0001_init … 0022_footer_cache
   ```
5. **GCP stack.**
   ```bash
   cd infra/gcp
   pulumi stack init hf --secrets-provider gcpkms://projects/oa-internal-450019/locations/global/keyRings/ops/cryptoKeys/pulumi
   git add Pulumi.hf.yaml && git commit -m '[hf] `gcp/`: stack `hf` initialized'
   pulumi preview -s hf   # expect creates only: hf-usage-job + 3 roles + its actor binding, repo hf-usage + reader, 7 secrets + 7 accessors, cron hf-usage-snapshot
   pulumi up -s hf
   gcloud scheduler jobs pause hf-usage-snapshot --project oa-internal-450019 --location us-central1   # until step 10
   ```
6. **Secret values** (names only here; nothing printed):
   ```bash
   infra/cf/hf-secrets -n -P -g                    # the plan
   infra/cf/hf-secrets -g                          # hf-usage-cf-token, -cf-account-id, -r2-endpoint, -r2-access-key-id, -r2-secret-access-key
   printf %s "$HF_TOKEN" | gcloud secrets versions add hf-usage-hf-token --project oa-internal-450019 --data-file=-
   # a Hub token with read access to the open-athena org (sees its private repos and buckets); `-L` history would need write
   gcloud secrets versions access latest --secret cw-s3-slack-bot-token --project oa-internal-450019 \
     | gcloud secrets versions add hf-usage-slack-bot-token --project oa-internal-450019 --data-file=-
   # (or another bot in #gcs-usage-alerts; with none, drop SLACK_BOT_TOKEN from job/hf-batch-submit.sh and the stack:
   #  Batch refuses to start a job whose secret has no version)
   # Pages, production (-P) and the dev stack's preview env (-d): STORE_ENDPOINT/ACCESS_KEY_ID/SECRET_ACCESS_KEY,
   # SESSION_SECRET, RESEND_API_KEY, MAIL_FROM. RESEND_API_KEY is in $oa/.envrc; carry it under another name, since
   # the inner `direnv exec` unloads what the outer one loaded:
   direnv exec ~/c/oa bash -c 'export HF_RESEND=$RESEND_API_KEY; cd ~/c/disky/wt/hf && direnv exec . bash -c "RESEND_API_KEY=\$HF_RESEND infra/cf/hf-secrets -P -d"'
   ```
   Then the Google OAuth client `hf.oa.dev (disky)` in project `oa-auth-509611` (as cw-s3's), origins `https://hf.oa.dev`, `http://localhost:3263`, `https://dev.hf.oa.dev`, `https://dev.oa-hf-usage.pages.dev`, each with its `/auth/google/callback`; after Create + Download JSON, the auth repo's `scripts/provision-oauth-client.mjs --project oa-auth-509611 --app-origin … --redirect-uri … --pages-project oa-hf-usage --from-json … --run` (production), and the same through a `--wrangler` wrapper appending `--env preview` for the dev stack (cw-s3's recipe, `specs/done/oidc-cutover-cw.md`).
7. **Image.** `job/build.sh` (Cloud Build into `hf-usage`; the build account needs Artifact Registry write on it, as cw-s3's builds into `cw-s3-usage`).
8. **Site.** `cd site && ./deploy -P` (first deploy, no push), and `./deploy --dev` (dev.hf.oa.dev). Both should show the sign-in wall; after sign-in, an empty store until the first scan.
8b. **The job's site grant** (`SITE_TOKEN`, for warm-up + probe): on hf.oa.dev `/admin`, a share link named `hf job (warm-cache)`, read-only, no email, no expiry; store its token without printing it: `printf %s '<token>' | gcloud secrets versions add hf-usage-job-grant --project oa-internal-450019 --data-file=-`. **Before any submit or the cron**: the job spec maps this secret, and Batch refuses to start a job whose secret has no version.
9. **First manual run.**
   ```bash
   job/hf-batch-submit.sh                         # prints the job id (DISKY_LABELS from the .envrc)
   gcloud batch jobs describe <id> --project oa-internal-450019 --location us-central1 --format='value(status.state)'
   ```
   Logs: the `logs_url` in `job/hf-scan-runs.json` (`labels.job_uid`). Expect `PHASE list` at ~10 min, the rest in minutes, `HF-SCAN-JOB-DONE <scan>`.
10. **Verify, then resume the cron.**
    - R2 holds `listing/<scan>/index/<gen>/path-index.parquet` (+ `-bysize`, age pyramid, search sidecars), `listing/<scan>/open-athena.{parquet,listing.json}`, `snapshots/hf/<scan>/{age,meta}.json`, `static-names/2026-10-14hf/`, `interval-store/2026-10-14hf/`.
    - The listing reconciles with billing: `open-athena.listing.json` `hf.expected` ≈ 15.72 TB (public + private), `hf.over` empty.
    - The layer-2 root (`path = '.'`): `sum_storage_class_id_1` ≈ 15.7 TB, `sum_storage_class_id_5` ≈ 0.02 TB, `size - sum_storage_class_id_6` = `hf.expected`.
    - D1: `SELECT date, variant, gen FROM index_schema` → `path`, `bysize` and the `age-pyramid-*` variants for `<scan>`; `SELECT scan, status, failed_phase, machine, cost_usd FROM scan_runs` → one `succeeded` run.
    - Site: the map renders the org (`buckets/`, `models/`, `datasets/`, `spaces/`); `/scans` shows the run; a `?f=` name search answers; `?ps=iv` renders the same view (the interval store); `dt-cloud healthcheck -u https://hf.oa.dev -s hf/` with a personal agent token (`/api/token`).
    - The interval store matches the per-scan reads (`dt-cloud interval-store verify`, and `?ps=iv` vs the default on subtrees, `?f=` and a diff once there are two scans); then `PATH_STORE = "intervals"` in both `[vars]` and `[env.preview.vars]`, and redeploy.
    - `gcloud scheduler jobs resume hf-usage-snapshot --project oa-internal-450019 --location us-central1`.
11. **Push** `hf` to `o` (with `site/deploy`'s pointer `hf-prod`), once the above is good.

## Gaps against the deployment baseline

`AGENTS.md` "Deployments: parity by default" is the baseline; these are hf's missing items, open work in this order:
- `PATH_STORE = "intervals"`: after the first scans verify (step 10).
- Cache warm-up and the serving probe: built (job steps 10–11, `probe -c` records under `probes/hf/`); they run once the job's grant exists (checklist step 8b).
- The Slack digest thread (cw-s3's `dt-cloud digest -T …`): needs a channel choice.
- The meta store ("data behind this site"): waits on the shared self-scan + index step the root session is building on `cloud`.
- Not applicable: staging / executor / sweeps (the site deletes nothing on the Hub), lifecycle pull (no lifecycle rules on the Hub).

## Hub-specific follow-ups

- UI: allowance bars / headroom (public and private storage vs `HF_ALLOWANCES`), an egress note; links from a file to its Hub page (`objectBases` can't express `models/<repo>/…` → `huggingface.co/<org>/<repo>/blob/main/…`).
- Measure the first runs (`/scans` cost and phases) and resize the machine; consider `-D` (repo-level dates, 1000 entries per tree page) if the listing outgrows its window.
- Old LFS versions per object (`bulk-list -L`) need a write token; today they're one `@history/@unlisted` row per repo.
- Least privilege as the `r2` branch does it: a scheduler account with only `batch.jobs.create`, so the job account needs no `batch.jobsEditor`.
