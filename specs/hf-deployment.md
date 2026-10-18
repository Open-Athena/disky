# hf.oa.dev: the Hugging Face deployment

Status: **live** (2026-10-15). First scan `2026-10-15`: 47 min, $0.11, 234,832 objects; billed bytes 15,753,134,621,714 B = the layer-2 root's non-git bytes exactly (billing page: 15.72 TB). Dev stack, site grant and cron all done. First scheduled scan `2026-10-16T0801` (2026-10-16, 44 min): every phase passed but `warm-cache` (Cloudflare 403'd urllib's default User-Agent in `top_paths`; fixed in `[cloud]` `b1930925`, image rebuilt, that scan warmed by hand, 32/32); the probe passed and the digest posted October's thread in #hf-usage (OP plot + TB replies for 10/15 and 10/16).

## What it is

A new OA deployment, modeled on cw-s3 (GCP Batch job → R2 + D1 → Cloudflare Pages), serving the Hugging Face org `open-athena` — its Storage Buckets and its model, dataset and space repos — as one store with one bucket (the namespace), at hf.oa.dev, to Open Athena staff.

| | name |
|---|---|
| GCP project / region | `oa-internal-450019` / `us-central1` (as cw-s3) |
| job service account | `hf-usage-job` |
| image | `us-central1-docker.pkg.dev/oa-internal-450019/hf-usage/scan:hf` (repo `hf-usage`) |
| cron | `hf-usage-snapshot`, daily 08:00 UTC, Batch on one `e2-standard-4`, on demand |
| secrets | `hf-usage-hf-token`, `hf-usage-cf-token`, `hf-usage-cf-pages-token`, `hf-usage-cf-account-id`, `hf-usage-r2-endpoint`, `hf-usage-r2-access-key-id`, `hf-usage-r2-secret-access-key`, `hf-usage-slack-bot-token`, `hf-usage-job-grant` |
| Pulumi stacks | `hf` in `infra/gcp` (project `gcs-usage-gcp`) and `infra/cf` (project `gcs-usage-cf`), backend `gs://oa-pulumi` |
| Pages project / domains | `oa-hf-usage` / `hf.oa.dev`, dev stack `dev.hf.oa.dev` (branch alias `dev`; OA Cloudflare account, zone `oa.dev`) |
| D1 / KV / R2 | `oa-hf-usage-db` (cw lineage) / `oa-hf-usage-cache` / `oa-hf-usage-index` |
| Slack | the HF Usage Bot app (`job/slack/`): the digest thread in #hf-usage (`C0C9G547AP7`), every alert in #hf-admin (`C0C9J9VDPV5`); icons Pages project `oa-hf-usage-icons` |
| cost labels | `app=disky,deployment=hf` (`$DISKY_LABELS`) |

## The daily job

Since 2026-10-17 the generic Nextflow pipeline (`specs/pipelines-everywhere.md`, as cw): the cron submits `dt-cloud scan head /app/job/profiles/hf.yml` (an e2-standard-2 head holding only the run record's D1 token + account id and the alert's Slack token), which runs `job/nf/scan.nf`, each step `dt-cloud scan step <name>` in its own Batch task that fetches its own secrets. Run records under `job/hf-scan-runs.json` (`/scans`: phases per task, the DAG, cost labels); every alert in #hf-admin. The steps:

1. `list`: `disk-tree bulk-list hf://open-athena` (Hub read token `HF_TOKEN`; per-file commit dates; 3 requests in flight; no `-P`, the Hub lister refuses it) into `gs://oa-hf-usage-data/raw/<scan>/open-athena/` (shards + `_SUCCESS.json`: `hf.expected` = Σ bucket size + Σ repo `usedStorage`, the request count), then `disk-tree import -e duckdb -m -p storage_class_id`: the layer-2 at `listing/<scan>/open-athena.parquet` with `sum_storage_class_id_{1,5,6}` (public, private, unbilled git). Rate-bound: ~4,000–4,600 requests at 1000 / 5 min (605 repos and buckets, 2026-10-17), ~38 min, its last ~20 min a few large repos paging through with no progress line.
2. `index`: `dt-cloud index-write` (the path store: `path` + `bysize` sorts, age pyramid, from the layer-2; no search sidecars, search is the static name index only) + `dt-cloud path-index` without `-P` (`options.index.webdata`: the snapshot JSONs `age.json`, `meta.json`).
3. `static-names` (one machine, `options.static-names.gen`: `add`, or `build -x 16,8` when the generation doesn't exist) into R2; `publish-r2` (a barrier: the path store and `meta.json` must be there) copies `listing/<scan>/` and `snapshots/hf/<scan>/` to R2 under the same keys.
4. `index-sync` (footers → D1, pointer last), `interval-store` (`add`, `build` the first time; R2, `CURRENT.json`), `index-gc`.
5. `post`: `warm-cache`, `probe -c`, the digest (`-T cw -C job/digest.yml`, `options.digest.root` on R2: the live thread state and the earlier snapshots are there).

The steps hand off through the data bucket `oa-hf-usage-data` (Nextflow's work dir `nf/`; lifecycle `raw/` 30 d, `nf/work|cache/` 14 d, `scratch/` 7 d); the site reads R2 only. The static-names generation and the D1 are the site's (`site/wrangler.toml`; `infra/tests/test_hf_stacks.py` checks the profile agrees); the interval store's generation is `CURRENT.json`'s for both.

Until 2026-10-17 the scan was `job/hf-run.sh`, the same CLIs in one e2-standard-4 VM under `dt-cloud scan-run supervise`, writing R2 only (no GCS); retired the same day, after the first cron-driven pipeline run (`2026-10-17T0802`) ran clean end to end (git history has it).

Decisions, and where they differ from what was asked:
- **GCS data bucket + R2 serving** (since the Nextflow switch, as cw): the steps need a shared `gs://` work dir and store; the site keeps reading R2. The one-VM job was R2 only, a simplification nobody asked for.
- **The path store comes from `index-write`, not `path-index -P`.** `path-index` re-aggregates the listing and carries only the GCS tier pivots (2–4), so the public / private / git split would be lost; `index-write` cuts the layer-2 as it is. `path-index` still writes the snapshot JSONs. Its `meta.json` `class_bytes` keeps public 1, private 5 and git 6 apart (`viz.CLASS_IDS`, since `[cloud]` `0d8e8a2c`; before, 5 and 6 folded into 1).
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
4. **D1 schema** (one current-state file, `site/schema.sql`; spec `done/d1-migrations.md`). A new database is created from it:
   ```bash
   cd site && pnpm exec wrangler d1 execute oa-hf-usage-db --remote --file schema.sql
   cd ../cloud && dt-cloud d1-schema check -d oa-hf-usage-db   # read-only: the live schema equals schema.sql
   ```
   `oa-hf-usage-db` itself was built (2026-10-15) by the 20 then-current `migrations/cw` files, `0001_init` … `0022_footer_cache`; `check` matches it to `schema.sql`. A later change is a recipe `site/migrations/<slug>.sql` (AGENTS.md "D1 schema"), applied here with `--file`, then removed.
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
   # (or another bot in #hf-admin; with none, drop SLACK_BOT_TOKEN from job/hf-batch-submit.sh and the stack:
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
- ~~`PATH_STORE = "intervals"`~~: done 2026-10-15 (first scan: `interval-store verify` 11/11 views equal). Diff parity is checked once there are two scans.
- ~~Cache warm-up and the serving probe~~: live since the first scheduled scan (2026-10-16); warm-up's root-view fetch 403'd on urllib's User-Agent that day, fixed (`[cloud]` `b1930925`).
- ~~The Slack digest thread~~: live since 2026-10-16 (October's thread in #hf-usage; the OP's sender is hf's mark, `op_icon: mark.png`, from November's thread on: `chat.update` can't change an existing OP's icon). Built 2026-10-15 (job step 12, `job/digest.yml`: the `cw` template, one reply per UTC day, % of the 253 TB public allowance, in decimal TB (`units: TB`, as HF bills); #hf-usage). It runs once these are applied, in order:
  1. The HF Usage Bot app from `job/slack/hf-usage-bot.manifest.json` (api.slack.com/apps → From a manifest), installed; its `xoxb-` token → `hf-usage-slack-bot-token` (replacing the copy of cw-s3's), the icon `deploy/slack/icons/hf.png`.
  2. `infra/cf/run up -s hf`: the icons Pages project `oa-hf-usage-icons` + the job's Pages token (`job_pages_token`).
  3. `infra/cf/hf-secrets -g`: now also `hf-usage-cf-pages-token`, after the GCP stack has its container: `pulumi up -s hf` in `infra/gcp` (+1 secret, +1 accessor; the cron body gains `SLACK_CHANNEL` + `CF_PAGES_TOKEN`). The secret needs its version before the next scheduled run: Batch refuses to start a job whose secret has none.
  4. The avatars on the icons project's production branch, once: `wrangler pages deploy job/icons-hf --project-name oa-hf-usage-icons --branch main`.
  5. The image (`job/build.sh`: now with wrangler, `job/digest.yml`, `job/icons-hf/`), then the cron (resumed after 8b).
  The site grant (8b) is needed too: the OP's plot is drawn by the site (`POST /api/digest-image`). Check a month's thread without posting: `dt-cloud digest -T cw -C job/digest.yml -n -r r2://oa-hf-usage-index/snapshots/hf` (with the R2 keys).
- ~~The meta store ("data behind this site")~~: done 2026-10-18, as `r2`'s. `job/profiles/hf-meta.yml` (`store: meta`) scans the data bucket `oa-hf-usage-data` (GCS) and the serving bucket `oa-hf-usage-index` (R2; listed through `$R2_ENDPOINT`, `[cloud]` 718f4555) into `meta-l2/` / `snapshots/meta/` and its own static-names generation `2026-10-18hfmeta`, served at `/meta` (`site/src/stores/meta.ts`, `STORES_EXTRA` / `STORES_JSON`; `/scans` links outputs there via `META_TREE_URL`). A secondary store is served per scan: no interval store, warm-up, probe or digest. Daily as its own head, the `hf-usage-meta` cron (09:15 UTC, `job/hf-meta-submit.sh`; applied 2026-10-18, +2); first run `hf-meta-scan-20261018-135304` by hand.
- ~~Share card~~: `site/public/og.jpg` is hf's generic card (`site/scripts/render-og-fallback.mjs`), with `OG_CARDS` / `OG_ANONYMOUS = "generic"` as cw.
- Not applicable: staging / executor / sweeps (the site deletes nothing on the Hub), lifecycle pull (no lifecycle rules on the Hub).

## Hub-specific follow-ups

- ~~UI: allowance bars / headroom, an egress note~~: done 2026-10-16. `Store.allowances` (`[cloud]` `009e0615`) meters public (class 1) vs 253 TB and private (class 5) vs 5.1 TB on the root view, from `meta.json` `class_bytes`, with the egress note: its 30 TB a month can't be measured from listings (the billing page shows it; the Hub has no API for it). The digest says the same (`[cloud]` `236702a9`: `quota:` as a list of class-scoped quotas), from the 2026-10-17 run on; the two pre-fix scans' `class_bytes` were backfilled from their layer-2 root rows.
- ~~Links from a file to its Hub page~~: done 2026-10-16. `Store.externalLink` (`[cloud]` `009e0615`; the deepest crumb's path card and the object panel), filled by `hubLink` in `site/src/stores/hf.tsx`: a repo's `tree/main/…` / `blob/main/…`, `@history` → its commits, buckets' `tree/…`, the org's tabs.
- Least privilege as the `r2` branch does it: applied 2026-10-16 (`pulumi up -s hf`, +6 / ~2 / −2; the cron's `oauthToken` account and the project bindings checked after). The cron calls Batch as `hf-usage-scheduler` (a custom role holding only `batch.jobs.create`, plus `serviceAccountUser` on `hf-usage-job`); `hf-usage-job` dropped `batch.jobsEditor` for `batch.jobsViewer` and stopped acting as itself; the Nextflow head took both back (below).
- Workflow: the generic Nextflow pipeline (`specs/pipelines-everywhere.md`; cw runs it since 10/16): `job/profiles/hf.yml`, `dt-cloud scan head` submitting each step as its own Batch task. Infra applied 2026-10-16 (`pulumi up -s hf`, +4 / ~2 / −1): the data bucket `oa-hf-usage-data` (Nextflow's work dir + the steps' store; `raw/` 30 d, `nf/work|cache/` 14 d, `scratch/` 7 d), `hf-usage-job` back on `batch.jobsEditor` + acting as itself (the head submits the step tasks), the cron's alerts to #hf-admin. Scratch run `hf-scan-20261017-021518` (7 tasks, ~11 min, outputs as laid out). Cutover 2026-10-17: D1 recipe `scan-run-nextflow` applied, the cron body applied (`scan head`), first prod run `hf-scan-20261017-024540` failed in `list` on one Hub `Connection reset by peer` (fixed: `[cloud]` `370cf635`, the lister retries connection errors like a 5xx); resumed as `hf-scan-20261017-030718`, it succeeded in 55 min (list ~43 min), but its digest found no icons dir (a task runs in its work dir; fixed: `options.digest.args: [-i, /app/job/icons-hf]`), so the next run's converge posts the missing day. The 08:00 cron run (`2026-10-17T0802`, ~60 min, every step) did: one OP, the 10-17 reply; that reply took the 02:47 re-run's numbers (`reply_hour: 0`), so `job/digest.yml` now has `reply_hour: 8` (the day's first scan from the cron's hour). `job/hf-run.sh` and `NF=0` retired. Scan triggers (`[cloud]` e5fc8f32): the cron body sets `DT_TRIGGER=schedule` (+ a `trigger` label), a hand submit `manual`; the run records `scan_runs.trigger`, and the digest (via `/api/scan-runs`) never replies from a scan whose first run was manual. Opt-in (`[cloud]` 55fc5178: unset records nothing; 7e56421b: D1 only, no per-scan file), so the image and site went first; opted in 2026-10-17: the `scan-run-trigger` recipe on `oa-hf-usage-db` (the 6 runs backfilled: `job-<uuid>` = Cloud Scheduler's = `schedule`, `hf-scan-*` = `manual`), then `pulumi up -s hf` (~1: the cron body).
- ~~Storage classes in the interval store~~: done 2026-10-18 (Ryan: each deployment's code in its own terms). The interval store and the static name index carry `private_bytes` (class 5) and `git_bytes` (class 6) in place of GCS's `c2`/`c3`/`c4`; public is the remainder. The site's `Row` follows, and the cell tip, owner bar and object panel name Public / Private / Git (the `?cl=` filter, `prg`, stays behind `prices`). Cut over: interval store `2026-10-18hf` (`CURRENT.json`; verify 94/94 over its 6 scans), static names `2026-10-18hfb` and meta's `2026-10-18hfmetab`. The old generations (`2026-10-14hf`, `2026-10-15hf`, `2026-10-18hfmeta`) are superseded. With the split held there, `scan retire` can take the layer-2s too (`-l`).
- Measure the first runs (`/scans` cost and phases) and resize the machine; consider `-D` (repo-level dates, 1000 entries per tree page) if the listing outgrows its window. Revisit both as Nextflow steps.
- Old LFS versions per object (`bulk-list -L`) need a write token; today they're one `@history/@unlisted` row per repo.
