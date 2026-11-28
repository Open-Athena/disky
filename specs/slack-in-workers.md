# Slack posting in Workers ("CFN mode")

Status: draft (2026-11-28, the r2 session, from Ryan's go to root: "yes go to build out CFN-mode for (all?) Slack posting"). Shared code, `[cloud]`; r2 is the first deployment to turn it on. Phase 1 is the digest: the scan's converge, an admin's switch, and the reapply's dry run run in Pages Functions, so no Batch job posts or plans a digest.

## Why

Today the digest thread is converged by the scan job's `digest` step (`dt-cloud digest`, Python + thrds, on Batch). Everything an admin does on `/admin/slack` that touches Slack waits on Batch too:
- **A switch** of a month's reply style (`PUT /api/digest-threads`) acts at the *next scan's* converge, up to a day later.
- **A reapply's dry run** is a Batch job (`DIGEST_REAPPLY`) on the scan image: a VM boot, an image pull and the scan's env for a few dozen Slack reads. Only its apply already runs in the site (`/api/digest-reapply/<job>/{plan,check,apply}`).

Each of these needs the Slack bot token, `CF_PAGES_TOKEN` (the plot's upload) and thrds in the job image. The site already holds the bot token (`SLACK_BOT_TOKEN`: activity posts, the staged card), the renderers (the OP's `opText` and the replies' `replies`, both byte-parity-tested against Python), the plot (`/api/digest-image`), and thrds's TS port (`@open-athena/thrds`: transport, rewrite plan/apply, D1 receipts, slots). So the digest can converge where its inputs already are. Batch keeps only the scan's own data work.

## Inventory: every Slack writer

| writer | today | what a Worker lacks | plan |
|---|---|---|---|
| **Digest converge**: the OP (post / edit, pin), each reply (post / edit; `weekly`: edited as each day lands), a provisional reply, the quota alert (post / edit / resolve), the split reply, the previous month's link line (`relink`), the preview bundle (`/admin/slack`) | the scan's `digest` step (Batch) | the month's **rows** (`digest_quota.load_month` / `rows_from_meta`: Python only, the snapshots' `meta.json`s); **plot hosting** (a `wrangler pages deploy` to the icons project's `plot` branch); the state's store on GCS for gcs / cw / hf (r2's is on R2, bound as `INDEX_R2`). Nothing a Worker can't do | phase 1 |
| **Thread switch** (`digest_switch.converge_thread`): the new style's replies posted, the old ones deleted (only those the reviewed plan lists) | inside the next converge | nothing: Python only | phase 1 (it runs at the PUT, not the next scan) |
| **Reapply dry run** (`digest -n -j`: the thrds rewrite plan, plus the switch plan for a style change) | Batch (`DIGEST_REAPPLY`) | nothing: thrds TS has `plan` / `findThread` | phase 1 |
| Reapply apply | site already | — | done |
| `digest-relink` backfill | a laptop CLI, one-off | — | stays a CLI (the converge relinks each new month itself) |
| **Discord digest twin** (`converge_discord`) | the scan's `digest` step | nothing (webhooks are plain HTTPS) | phase 2; a deployment with Discord keeps its Batch step until then |
| **Run thread** (`nominal`, `#<deploy>-admin`: ⏳ on start, the outcome on finish, quiet / monthly threads) | `scan-run supervise` in the job | the outcome oracle is already the site's run summaries (D1 `scan_runs`); the writer's lease (an object-generation lease) becomes D1. About 1,900 lines of Python to port | phase 3: the site converges a run's thread on each run-record write, so the job posts nothing |
| **Staged-plan card** | site (`/api/plans/staged/card`; the job's `staged-card` step only POSTs) | — | done: phase 1's pattern |
| **Activity posts** (`#<deploy>-admin`) | site (`_lib/activity.ts`) | — | done |
| **Alerts** (`disk-tree notify` / `push`: a failed phase, a stuck scan, `scan-events`'s compaction alerts) | jobs, the `scan-events` service | nothing technical | **stay out of band**: an alert has to reach someone when the site is the thing that's broken. They're a single stdlib POST each, from wherever the failure is seen |
| **Monitoring relay** (Cloud Monitoring incidents → Slack) | Cloud Run | nothing (Pub/Sub push to a route; Google's OIDC token checked with WebCrypto) | **stays**: it relays the site's own uptime incidents |
| Emoji check / install | CLI + skill (reads; the upload is a person's) | — | stays |

So "all" means: every writer whose trigger is the site's own data moves in. Alerts and the relay stay outside the site on purpose.

## Design

### Where the work runs: a step route over a D1 job row, driven by whoever asked

A **Slack job** is a D1 row (`slack_jobs`): `id`, `kind` (`digest-converge` | `digest-switch` | `digest-plan`), `key` (`<channel>/<YYYY-MM>`: one live job per thread), `args` (JSON), `state` (`pending` | `running` | `done` | `failed`), `cursor` (JSON: the converge's own progress), `lease_until`, `retry_at`, `calls`, `error`, `created_by`, the timestamps. Each Slack call the job makes is recorded first in thrds's D1 receipts (`@open-athena/thrds/d1`, as the reapply's apply already does), so a call whose answer was lost is never repeated blind.

`POST /api/slack-jobs/<id>/step` takes the job's lease (a conditional `UPDATE … WHERE lease_until < now`) and runs it until one of these:
- it's done;
- it has made **N Slack calls** (`SLACK_STEP_CALLS`, default 40);
- **~20 s of wall time** have passed;
- Slack asks it to wait longer than 10 s (`Retry-After`), recorded as `retry_at`.

It then persists the cursor, releases the lease and answers `{state, calls, retry_at?, done}`. Whoever started the job **drives it**: it POSTs `step` again until `done`.

- **The scan job**: its `digest` step, under `options.digest.cfn`, becomes `dt-cloud digest-cfn`. That's one `POST /api/digest/converge` (creating or joining the thread's job), then `step` until done, a few minutes at most. It needs `$SITE_TOKEN` only: no Slack token, no `CF_PAGES_TOKEN`, no thrds.
- **`/admin/slack`**: a switch's PUT or a reapply's "dry run" creates the job and the page drives it, showing progress.
- **Nothing drives a job**, because its driver died: the job stays `pending` / `running`, past its lease. The thread's next converge finishes it first (one live job per `key`). `/admin/slack` lists it with a "Resume" button.

Every kind is a **converge**: it reads what's posted (state + Slack) and moves it toward what should be. So a resumed or repeated job is correct, never a duplicate post (receipts cover the window between a post and its state write).

**Why this driver, against the alternatives** (Cloudflare's limits as documented 2026-11; Pages takes the Workers Standard limits and `[limits] cpu_ms`):
- **One request doing everything:** a request's wall time is unlimited while the client stays connected, and the subrequest cap is 10,000. A whole switch fits in one request: ~30 deletes and ~5 posts, about a minute at Slack's pace. But one dropped connection then loses an unknown point in the middle. Chunks with a persisted cursor make each step short and every interruption a resume, not a re-plan.
- **A self-invoking chain** (`waitUntil` fetching the next step): `waitUntil` runs only 30 s past the response. A Worker fetching its own hostname isn't documented to work: "Using global fetch() to call another Worker on the same zone without service bindings fails." It's also an unbounded loop with no driver to see it fail.
- **Queues, cron triggers, Durable Object alarms:** Pages can't consume a queue ("You cannot currently configure a queues consumer with Pages Functions") and has no cron triggers. A DO needs its own Worker. Each means a second Worker per deployment, with its own IaC, deploy and secrets. Worth it only for a writer that must act with nobody asking, and none here does: the scan, the page and the run record are each the natural driver.
- **CPU:** the expensive part of a converge is the plot (resvg, ~1–3 s), well under the 30 s default. The month's rows are ~60 `meta.json` reads, and the Slack calls are wall time, not CPU.
- **Slack's rate limits are the real pace:**
  - `chat.postMessage` is about one a second per channel;
  - `chat.update` / `chat.delete` / `conversations.replies` are Tier 3 (~50/min);
  - `pins.add` is Tier 2.

  thrds's transport honors `Retry-After`. N = 40 keeps a step near 30–50 s even at Tier 3's pace, and a step never sleeps past 10 s: it records `retry_at`, and its driver waits.

### The converge's inputs, in the Worker

- **Config:** the deployment's `job/digest.yml` as JSON in a var, `DIGEST_CONFIG`. A test checks it equals the yml (`dt-cloud digest config-json job/digest.yml`), so the Batch path and the Worker read one config while both exist. The channel is `SLACK_DIGEST_CHANNEL`; the token is the site's `SLACK_BOT_TOKEN` (the same deployment bot).
- **Rows:** `loadMonth`, a TS port of `digest_quota.load_month` (scans listed under `root`, the window, `read_metas`, `rows_from_meta`), with each scan's trigger read straight from D1 `scan_runs` (the job's `site_triggers` asks the site for the same rows). It reads over a `SnapshotStore`: R2 (a binding) first; GCS (through the site's `GCP_SA_KEY`) for gcs / cw / hf, in phase 2. Parity is golden fixtures: the Python `rows_json` of a fixture month, byte-equal to TS's (CI runs both suites).
- **State:** the same per-month JSON at the same key (`<root>/../digest/<channel>/<variant>/<YYYY-MM>.json`), so the Python and TS converges are interchangeable: turning CFN mode on or off is a flag, never a migration. Writes are compare-and-swap (R2 `onlyIf: {etagMatches}`; GCS `ifGenerationMatch`). A lost race re-reads and re-plans.
- **The plot:** drawn by `/api/digest-image`'s code in-process. Its PNG is stored by content hash in `public_avatars` and served at `/a/<sha256>.png`, the public route that exists for exactly this: bytes a Slack post used, named by their own hash, no listing. That replaces the icons project's `plot` branch and `CF_PAGES_TOKEN`. A new plot is a new URL, so Slack's image cache never shows a stale one. Its size is checked against D1's row limit (refused over 1.5 MB; today's are ~100–300 KB).
- **Renderers:** `opText`, `imageRequest` and `replies` (already in `site/src/digest/`, parity-tested). The quota alert's body and its resolved note (`digest_quota.alert_body` / `resolved_note`) are ported with goldens.
- **Not in phase 1**, refused by name so a config using it stays on Batch: the split reply (`split_reply: true`), Discord (`discord_webhook_env`), the `cost` template. r2 uses none of them.

### Turning it on

Per deployment:
- the site var `DIGEST_CFN = "1"` (plus `DIGEST_CONFIG`, `SLACK_DIGEST_CHANNEL`);
- the scan profile's `options.digest.cfn: true`;
- D1 recipe `slack-jobs`.

With `DIGEST_CFN` unset, nothing changes: the routes answer 503, the switch keeps waiting for the next scan, and the reapply's dry run stays a Batch job.

**The dev stack never writes the prod thread.** Its converge runs as a **shadow**: the same job computes the calls it would make and the bodies it would post, and writes them to the job row, with no Slack writes. `GET /api/slack-jobs/<id>` shows them beside what the Batch converge posted for the same scan (the preview bundle), body for body.

## Phase 1: the digest, r2 first

Built so far (2026-11-28, `[cloud]` commits on r2):

1. **D1 recipe `slack-jobs`**: `slack_jobs`, one live row per `key` (a partial unique index). Receipts aren't needed for a converge: its state is written after each post, as the job's is, so a post whose answer was lost is the only gap, the same one the job has.
2. **`loadMonth` in TS** (`src/digest/load.ts`, `functions/_lib/digestStore.ts`): goldens from Python's `load_month` → `rows_json` over a fixture listing (`cloud/tests/fixtures/digest/load/months.json`: date-only ids sharing a day, a gap, `as_of`, a first month, another primary, TB and TiB). `SnapshotStore` has R2 (a binding, `onlyIf` etag CAS) and GCS (the JSON API under `GCP_SA_KEY`, `ifGenerationMatch`) from the start; `load_month` took a `now`, so its slugs are fixed.
3. **The `weekly` replies in TS** (`quotaReply.ts`): `replies()` had only `sender` and `body`, and r2 is `weekly`. Goldens added to the replies' set. `tb()` read a delta rounding to zero from below as `−0.0`; it's `+0.0`, as Python's `_tb` reads it.
4. **The payloads as thrds sends them** (`functions/_lib/digestWire.ts`): thrds TS has no markdown → mrkdwn converter, so the locked thrds's (`uv.lock`: `b3a1690`) `to_slack`, trailing-image lifting, `{bust}`, the 4,000-character refusal and an edit's `blocks: []` are ported, with goldens captured from it for every OP and reply fixture (`fixtures/digest/wire/payloads.json`).
5. **The converge** (`functions/_lib/digestConverge.ts`): `converge_slack` (OP, pin, `link_prev` through thrds TS's rewrite `plan`/`apply`, the dropped split reply, the preview bundle, replies), the thread states and lease keys, `desired_variant`, shadow writes, a per-job `memo` (the OP isn't edited twice in one job). The plot is drawn in-process (`/api/digest-image`'s `renderDigestImage`), stored in `public_avatars` and served at `/a/<sha256>.png`; one over 1.4 MB (a D1 row, as base64) is refused, never cut.
6. **The job runner and routes** (`functions/_lib/slackJobs.ts`): `POST /api/digest/converge` (the grant `DIGEST_CFN_GRANT` names: the scan job's), `GET /api/slack-jobs[/<id>]`, `POST /api/slack-jobs` and `/<id>/step` (admin). A step takes the job's own lease (one step at a time), then the thread lease the job's converge takes (`digest_switch.thread_lease`'s record, at its key: a Worker and a job never converge one thread at once), stops at `SLACK_STEP_CALLS` (40) calls or 20 s, and turns a `Retry-After` past 10 s into `retry_at`. A shadow (`DIGEST_CFN = "shadow"`) audits every message the thread's state names against what it renders, wire text to wire text; `/admin/slack` lists the jobs with their audits ("Site converges").
7. **`dt-cloud digest-config`** (the `DIGEST_CONFIG` var's line) and **`dt-cloud digest-cfn`** (steps the job, prints a shadow's audit); the `digest` step's `cfn` (drive the site's converge instead of posting) and `shadow: <dev URL>` (a shadow after the job's own converge).

Found on the way: `digest._op_render` asked only the run's own style for the previous month's OP, so r2's December thread (`weekly`, after November's `sender`) would have lost its `← November` link. Fixed in the job too (`[cloud]` 2334998a2, before December 1).

Still refused by name (the job keeps converging these): a switch, a retiring style, provisional replies, the quota alert, the split reply, Discord, the `cost` template. Next:

8. **The switch in TS**, a port of `digest_switch.converge_thread`: post the new style, retire, delete only the row's `deletes`, edit only its `edits`. A switch's PUT creates the job, and the page drives it right away.
9. **The reapply dry run in TS**: the rewrite plan (thrds `plan`) and the switch plan, in the formats the page already reads (`switch.json`'s `PLAN_VERSION` 1), stored where the apply reads them.
10. **r2 rollout:**
    1. Dev stack in shadow (`DIGEST_CFN = "shadow"`, `DIGEST_CONFIG`, `SLACK_DIGEST_CHANNEL`, `DIGEST_CFN_GRANT` = the job's grant; the profile's `digest.shadow: https://dev.r2.oa.dev`) for a few scans: each audit all `same`.
    2. With Ryan's go, prod: `DIGEST_CFN = "1"` and the profile's `cfn: true` in one ship. The Batch digest's tokens (`CF_PAGES_TOKEN` in the digest step) then come out of r2's profile.
    3. The switch and reapply move with it.
    4. hf next (its state is on GCS), then cw, then gcs.

## Phase 2

GCS `SnapshotStore`; the split reply; the `cost` template's converge; the Discord twin; then every deployment's Batch `digest` step removed, along with `DIGEST_REAPPLY`'s Batch path, the icons project's `plot` branch, and thrds and the digest from the scan image.

## Phase 3

The run thread (`nominal`): the site converges a run's thread when its run record is written (`/api/scan-runs` writes), as one Slack job per thread. The object-generation lease becomes the job row's. `scan-run supervise` then posts nothing to Slack.

## Open questions

- `DIGEST_CONFIG` as a var duplicates `job/digest.yml` until phase 2 retires the Batch path; a CI check keeps them equal. Alternative: the site build reads the yml directly (a Vite/wrangler define). Either is fine. The var is simpler to see on `/admin/slack`.
