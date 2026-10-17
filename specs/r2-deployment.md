# OA R2 (r2.oa.dev): baseline gaps

The deployment's launch is [`done/r2-launch.md`]. This spec tracks the CLAUDE.md "parity by default" baseline items r2.oa.dev doesn't have yet, and how each lands.

## Job baseline (2026-10-16)

The daily job (`job/r2-scan`, a Python driver on GCP Batch, 07:15 UTC) records each run (`/scans`) and appends the static name index and interval store. Built on `r2` (not yet in an image):

| item | how | needs |
|---|---|---|
| Run record + failure alerts → #r2-admin | the shared job wrapper: `ScanReporter` drives `dt_cloud.scan_runs.JobRun` in process (`scan-run start` / `phase` / `finish`'s steps: the D1 record, PHASE lines, `disk-tree notify` redacted of `r2-scan-runs.json`'s `secrets` with a Batch logs link): `fail` once per failed run, naming the phase; `warn` on a failed non-fatal step (an index stage, index-gc, a serving step) | `SLACK_BOT_TOKEN`, `slack_alert_channel` (C0C9U47KTM2) |
| Killed runs (preemption, OOM) | the image's entrypoint is `dt-cloud scan-run supervise -- python job/r2-scan run`: the reporter points it at its run, and a run killed before its own exit is recorded failed (`terminated (SIGTERM)` / `killed (SIGKILL), most likely out of memory`) and alerts once. Index-only and age-backfill jobs override the entrypoint and aren't supervised | — |
| `index-gc` | after the meta phase, per store (primary, meta): `dt-cloud index-gc -s <store>` (D1 row groups of unpointed generations), then the serving bucket's unpointed generation dirs older than 2 days. Those R2 deletions are gated: until `index_gc_files` is `true` in `job/r2.json` the dirs are only listed (`-R -n -F r2://oa-r2-usage-index`, in the log). Dry run 2026-10-16 (`tmp/r2-index-gc-dryrun.txt`): 0 row groups, 0 dirs in both stores (every generation dir is pointed: 8 primary scans, 4 meta) | — |
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
6. **Channels**: done — #r2-usage `C0C9J6CE8BD`, #r2-admin `C0C9U47KTM2` (alerts, and any staging, DR or deletion threads; renamed from #r2-usage-alerts 2026-10-16), in `job/r2.json`; #r2-usage carries the digest only. `/invite @R2 Usage Bot` in either if private (public ones need no invite: `chat:write.public`).
7. **Site grant**: mint a read-only grant at https://r2.oa.dev/admin, then `gcloud secrets versions add oa-r2-site-grant --project oa-internal-450019 --data-file=-`.
8. **Go live**: list all three in `optional_secrets_live`, build the image (`job/r2-scan build <tag>`), pin it in `infra/gcp/Pulumi.r2.yaml` (`r2:scanImage`), and `pulumi up -s r2` in `infra/gcp` again. The next scan warms, probes, posts the day's digest reply, and alerts on any failure.

Steps 1–4 can go before Slack exists; step 8 must follow 3, 5 and 7, since a listed secret without a version stops the job from starting.

## Still open

- `index_gc_files` (R2 deletion of unpointed generation dirs) stays off until Ryan reviews a run's `index-gc-files` list.

[`done/r2-launch.md`]: done/r2-launch.md

## The switch to the Nextflow pipeline ([`pipelines-everywhere.md`] §3.5)

Profiles in `job/profiles/`: `r2.yml` (primary), `r2-meta.yml` (`store: meta`, a second head), `r2-scratch.yml` (the scratch run: the four buckets into `oa-r2-usage-scratch`, no serving credentials, nothing recorded). The run record is `job/r2-nf-scan-runs.json` (outputs at the data store's layout keys). The image (`scan:nf`) carries Nextflow beside the one-VM entrypoint; `job/r2-scan submit -H job/profiles/<p>.yml -n <name> <image@digest>` submits a head.

What changes against the one-VM job:
- per-call keys: the source key for `list` only (`options.list.secrets`), the serving key everywhere else, `CF_PAGES_TOKEN` for the digest only;
- `bucket-check` skipped (r2 scans four approved buckets of a larger account);
- the digest reads `r2://oa-r2-usage-index/snapshots/r2` (`options.digest.root`), where its thread state and every earlier scan are;
- new for r2: over-time groups (`over-time`), the run's DAG on /scans;
- not carried: the age pyramids' `.groups` sidecars (no reader) and the `gs://oa-r2-usage-data/scans/<scan>/<gen>/` archive (the data store's layout is the archive); probe records go to `gs://oa-r2-usage-data/probes/r2/` instead of R2.

Steps, in order (each a go from Ryan):
1. `infra/gcp` `pulumi up -s r2`: the job account gets `roles/batch.jobsEditor` and acts as itself (a head submits its tasks' jobs as it).
2. Scratch run; compare its `listing/<scan>/index/<gen>/` and `snapshots/r2/<scan>/` against the same day's one-VM scan.
3. A prod run of both heads (primary, then meta) beside the cron, then the scheduler: `r2:heads` in `Pulumi.r2.yaml` (`[{profile, schedule}]`) gives each head its own scheduler job (`r2-daily` keeps the first; each head is its own Batch job, so its own run record), its body `job/r2-scan plan -H job/profiles/<p> <r2:scanImage>`. The head is pinned by digest (`r2:scanImage`); `plan -H`/`submit -H` set `DT_SCAN_IMAGE` to the same digest, so every task runs the head's image (the profiles' `scan:nf` tag is only the fallback: a profile baked into the image can't name its digest).
4. `job/r2-scan run` and its helpers are deleted after a clean week.

[`pipelines-everywhere.md`]: pipelines-everywhere.md
