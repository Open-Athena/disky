# OA R2 (r2.oa.dev): baseline gaps

The deployment's launch is [`done/r2-launch.md`]. This spec tracks the CLAUDE.md "parity by default" baseline items r2.oa.dev doesn't have yet, and how each lands.

## Job baseline (2026-10-16)

The daily job (`job/r2-scan`, a Python driver on GCP Batch, 07:15 UTC) already records each run (`/scans`, `ScanReporter` over `dt_cloud.scan_runs.record`) and appends the static name index and interval store. Built on `r2` (not yet in an image):

| item | how | needs |
|---|---|---|
| Failure alerts → #r2-usage-alerts | `alert()` posts through the shared wrapper's `scan_runs.JobRun.alert` (`disk-tree notify`, redacted of `r2-scan-runs.json`'s `secrets`, a Batch logs link): `fail` once per failed run, naming the phase; `warn` on a failed index stage or serving step | `SLACK_BOT_TOKEN`, `slack_alert_channel` in `job/r2.json` |
| Cache warm-up | `dt-cloud warm-cache -d <scan> -r r2://oa-r2-usage-index/snapshots/r2`, its own phase, never fatal | `SITE_TOKEN` |
| Serving probe | `dt-cloud probe -c -s r2 -u https://r2.oa.dev -o r2://oa-r2-usage-index/probes/r2/`, never fatal | `SITE_TOKEN` |
| Slack digest → #r2-usage | `dt-cloud digest -T cw -C job/digest.yml` (one reply per UTC day, decimal TB as the site renders, no quotas); state `r2://oa-r2-usage-index/digest/r2/…`; plot on `oa-r2-usage-icons`'s `plot` branch; avatars = `job/icons-r2/` (the shared arrows) on its production | `SLACK_BOT_TOKEN`, `slack_channel`, `SITE_TOKEN`, `CF_PAGES_TOKEN` |

A step whose credentials are missing is skipped with a line in the log, so the job runs unchanged until each is supplied.

**Optional secrets.** Batch refuses to start a job whose secret has no version, so `job/r2.json` splits them: `secrets` (always passed) and `optional_secrets` (`SLACK_BOT_TOKEN` → `oa-r2-slack-bot-token`, `SITE_TOKEN` → `oa-r2-site-grant`, `CF_PAGES_TOKEN` → `oa-r2-cf-pages-token`). The GCP stack creates every container; the job spec passes an optional one only once `optional_secrets_live` lists it, i.e. after its version exists.

Dry digest over prod snapshots (2026-10-16, `-n`, no plot without a site grant): headline `+0.1 TB` month-to-date, `288 TB`; replies 10/12–10/15 at `288 TB` each.

## Ryan's steps, in order

1. **GCP stack** (`infra/gcp`): `pulumi up -s r2`. Creates the three empty secret containers + accessors; the scheduler body gains `SITE_URL` (harmless to the current image). Safe before anything else exists.
2. **CF stack** (`infra/cf`): `pulumi up -s r2`. Creates the `oa-r2-usage-icons` Pages project and the job's Pages token (`job_pages_token`).
3. **Pages token → GCP**: `infra/cf/sync-secrets -g` (also re-installs the existing job secrets from the stack outputs, unchanged) adds `oa-r2-cf-pages-token`'s version.
4. **Avatars**: `wrangler pages deploy job/icons-r2 --project-name oa-r2-usage-icons --branch main` (OA account token, as the site deploys).
5. **Slack app**: api.slack.com/apps → Create New App → From a manifest → the OA workspace → paste `job/slack/r2-usage-bot.manifest.json` → Create → Install to Workspace. Upload `site/public/icons/r2-512.png` as the app icon (Basic Information). Store the `xoxb-…` token: `gcloud secrets versions add oa-r2-slack-bot-token --project oa-internal-450019 --data-file=-` (paste, then Ctrl-D; never anywhere else).
6. **Channels**: create #r2-usage (digest) and #r2-usage-alerts (alerts). Public channels need no invite (`chat:write.public`); `/invite @R2 Usage Bot` in any private one. Put both channel ids in `job/r2.json` (`slack_channel`, `slack_alert_channel`).
7. **Site grant**: mint a read-only grant at https://r2.oa.dev/admin, then `gcloud secrets versions add oa-r2-site-grant --project oa-internal-450019 --data-file=-`.
8. **Go live**: list all three in `optional_secrets_live`, build the image (`job/r2-scan build <tag>`), pin it in `infra/gcp/Pulumi.r2.yaml` (`r2:scanImage`), and `pulumi up -s r2` in `infra/gcp` again. The next scan warms, probes, posts the day's digest reply, and alerts on any failure.

Steps 1–4 can go before Slack exists; step 8 must follow 3, 5 and 7, since a listed secret without a version stops the job from starting.

## Still open

- `index-gc` after each scan (baseline) is not run by `job/r2-scan`.
- The job is a bespoke Python driver, not `dt-cloud scan-run supervise` (preemptions / OOM kills aren't recorded as a failed run, so they don't alert).

[`done/r2-launch.md`]: done/r2-launch.md
