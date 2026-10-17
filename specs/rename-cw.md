# Rename the deployment: cw-s3 → cw

Status: phase 1 done 2026-10-17 (cw.oa.dev + dev.cw.oa.dev serve; the old hosts 301 GETs; sign-in, job links, Slack app URL + description, channels renamed). Phase 2 done 2026-10-17 except the worktree dir: deployment label `cw` (profiles, cost labels), Pulumi stacks `cw` (cf component pinned `cw-s3`), branch `cw` / `cw-prod` / `cw-dev` (old `o` branches kept a week). Left: `wt/cw-s3` → `wt/cw` (Ryan, between sessions: `cmv`, then `git worktree repair`).

"s3" names how we read CoreWeave's storage (its S3-compatible API, CAIOS), which nobody using the site or reading the digest needs. The display name is already "Marin CoreWeave".

## Scope

| row | what | examples | plan |
|---|---|---|---|
| 1 | user-facing | `cw-s3.oa.dev`, `dev.cw-s3.oa.dev`, `#cw-s3-usage`, `#cw-s3-admin`, the bot's description | rename (phase 1) |
| 2 | deployment identity | branch `cw-s3` (+ `cw-s3-prod`, `cw-s3-dev`), the deployment label (`deployment=cw-s3` on every Batch job, scan-run profile name, scan profile `deployment`), `job/profiles/cw-s3.yml`, Pulumi stacks `cw-s3` (cf, gcp), the worktree `wt/cw-s3` | rename (phase 2) |
| 3 | cloud resources | buckets `oa-cw-s3-usage-{data,index,scratch}`, D1 `oa-cw-s3-usage-db`, KV, Pages project `oa-cw-s3-usage`, SA `cw-s3-job`, secrets `cw-s3-*`, Artifact Registry `cw-s3-usage` | keep: buckets can't be renamed (only copied), the rest is churn with no visible gain; they're internal ids, like gcs's `gcs-usage` ones |

## Phase 1: what people see

1. **Domain.** `cw.oa.dev` becomes production and `dev.cw.oa.dev` the dev stack; `cw-s3.oa.dev` and `dev.cw-s3.oa.dev` stay attached and redirect (301, path + query kept), so every link already posted (Slack threads, the digest, bookmarks) keeps working.
   - `infra/cf`: the shared `cfn_dashboard.py` (byte-identical across branches; a `[cloud]`-style change through root) gains `legacy_domains` (PagesDomain + CNAME each, like a branch alias but onto production); the stack's `domain: cw.oa.dev`, `branchAliases: [{domain: dev.cw.oa.dev, branch: dev}]`, `legacyDomains: [cw-s3.oa.dev, dev.cw-s3.oa.dev]`. `pulumi up` (needs a go).
   - Site: a middleware redirect for a request whose host is in `LEGACY_HOSTS` (wrangler var) to `PROD_HOST` / `DEV_HOST`; `PROD_HOST = "cw.oa.dev"`, `DEV_HOST = "dev.cw.oa.dev"`, `index.html`'s og URLs, `META_TREE_URL`.
   - Sign-in: the Google OAuth client gets the new origins + callbacks (`https://cw.oa.dev/auth/google/callback`, dev's); Ryan adds them in the Cloud console (no API). Sessions are per host, so everyone signs in once on the new host. Email-code links built from the request origin follow on their own.
   - Job: `SITE_URL` (profile env, `cw-env.sh`, `cw-meta-run.sh`), `job/digest.yml` `site_url` (+ `test_digest_cw_config.py`), `cw-dogi.py` / `cw-isolation-credentials.py` hosts, the Slack app's interactivity `request_url` (`/slack/actions`; Ryan updates the app config, or the manifest is re-applied).
   - Order: CF domains attached (new host serves) → OAuth origins added → site deployed with the redirect and new hosts → job image + cron (SITE_URL) → Slack request URL. Each step leaves both hosts working.
2. **Slack channels.** `#cw-s3-usage` → `#cw-usage`, `#cw-s3-admin` → `#cw-admin`. Slack keeps channel ids (`C0C1YR7D0KU`, `C0C53CPM886`), so no config changes; Ryan renames them (the bot has no `channels:manage`). Comments naming the channels updated.
3. **The bot's description** (manifest): "… Marin CoreWeave buckets — cw.oa.dev".

## Phase 2: the deployment's identity

1. **Branch**: `cw-s3` → `cw`, `cw-s3-prod` → `cw-prod`, `cw-s3-dev` → `cw-dev` on `o`; `site/deploy`'s pointer (`PTR_SHORT`, `PUSH_REMOTE` in `site/deploy.env`); CI triggers on the branch name; root's cherry-pick tooling and the sessions' notes. The old remote branches deleted after a week.
2. **Deployment label**: `deployment=cw` on every job (`labels` in the scan profile, `$DISKY_LABELS` from the gcp stack), the scan-run profile `name`, the scan profile's `deployment` (the Nextflow session namespace `disky/<deployment>/scan/<scan>`: a scan in flight across the switch restarts rather than resumes). Billing/cost reports by label get a discontinuity at the switch; the cost-label spec notes the date.
3. **Files**: `job/profiles/cw-s3.yml` → `cw.yml`, `cw-s3-scratch.yml` → `cw-scratch.yml`; `cloud/tests/fixtures/scan_profiles/cw-s3.*` stays (a cloud fixture, cw-shaped).
4. **Pulumi stacks** `cw-s3` → `cw` (cf and gcp: `pulumi stack rename`, state moved, no resource changes; resource names inside stay).
5. **Worktree** `wt/cw-s3` → `wt/cw` (the session's own project dir follows the path; move with care: untracked `tmp/` and the Claude project memory).

## Open

- Whether `CLAUDE.md`'s "cw-s3" section heading and the specs' history mention the old name (history stays; current-state text moves).
