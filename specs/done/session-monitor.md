# Temporary GCS session monitor

Approved and live 2026-10-13: check production session logs every five minutes through October 20 UTC and alert only on problems, without creating notification spam. Production deployment `6d892cb6`; the live dry-run and first clean stateful check succeeded without posting to Slack. The dedicated scheduler is enabled.

## Contract

`POST /api/session-monitor` requires exactly `gcs:session-monitor:read` (or an explicit wildcard grant), not ordinary viewer/admin access. This credential cannot read the raw session list/timeline, viewer APIs, assignments or deletion dispatch. A separate named grant is stored in Secret Manager; D1 retains only its hash. Both grant and endpoint expire at 2026-10-21T00:00:00Z. Turning session logging off also makes checks no-ops. Expired scheduler calls return an unauthenticated, no-DB-access `expired` response, so expiry cannot create auth-failure alerts.

Production requires `SESSION_MONITOR=1` and a request to `PROD_HOST`; preview sets `0` and only permits `?dry=1`. Dry-run reads aggregates but never changes state or calls Slack. No new general app or cloud-admin credential is given to the cron.

The monitor queries batches by indexed server receipt time in the last ten minutes, not client clocks. At most 200 batches are processed; exceeding that budget fails the check without sending a partial-count alert. A single uncaught error/rejection, at least three network/server/console failures, or at least three successful requests taking ≥10 seconds triggers an incident. Ordinary cancellations (`err=abort`), 4xx responses and isolated slow requests do not trigger alerts. This is a recorded-client-event monitor, not exhaustive uptime monitoring: logging starts on page load, not retroactively or in already-open unlogged tabs; lost beacons and client caps can omit events. The existing hourly independent health workflow remains in place.

## Noise and privacy limits

- Globally at most three Slack notification attempts per UTC day, at least one hour apart, with new error/slow-request evidence required after the previous alert. A continuing incident uses its original thread, never broadcasts replies, mentions anyone or sends recovery/healthy messages. Thirty quiet minutes close the incident silently. UTC midnight replenishes the daily budget but does not bypass the hourly cooldown.
- `session_monitor` is a single additive D1 state row, with an optimistic version CAS. Attempts and cooldown are committed **before** Slack I/O. Overlapping requests cannot send duplicates. A timed-out, failed or ambiguously delivered Slack call consumes the same budget as success and is not retried immediately; the 90-second lease survives abrupt termination. No automatic alert retries are enabled in Cloud Scheduler.
- Repeated calls less than four minutes apart do no aggregate scan. Slack calls have an eight-second deadline; Scheduler has a thirty-second attempt deadline.
- Slack receives only counts and a fixed authenticated [admin sessions] link. No emails, user/session IDs, paths, filter text, URLs from events, messages, stacks or credentials leave the aggregate query. Monitor failures return a fixed error, never a SQL/body dump, and cannot recursively alert about themselves.

## Operations

Tracked deployment profile: `job/session-monitor.json`. `dt-cloud session-monitor` supports `provision`, `install`, `dry-run` (default), `check`, `audit`, `status` and `pause`. `-d` is accepted only for a dev dry-run. Install first provisions/reuses the expiring narrow grant, verifies a successful live production dry-run, then creates/updates only the dedicated `gcs-session-monitor` HTTP scheduler. Existing schedulers with a different target are refused. Raw credentials are never passed via argv or printed; the scheduler target header and Secret Manager contain the credential, so access to those GCP resources should remain restricted.

Provision/install require the deployment's existing CF credentials and explicit `D1_DB_ID`/`D1_DB_NAME` environment variables (the generic Python CLI does not infer another deployment's database from a checkout). Other operations need only GCP credentials to retrieve the dedicated token/status.

The calendar cron `*/5 * 13-20 10 *` has no executions after October 20 this year. Cloud Scheduler cron has no year field; the endpoint's fixed absolute expiry prevents future-year scans/alerts even if the inert resource remains. Pause/remove the dedicated scheduler during cleanup; do not widen dates or reactivate the grant automatically. The imperative CLI/profile owns this temporary resource; it has not been added to or applied by Pulumi. No daily scan, VM or existing cron was changed.

## Verification

Twenty-one exact TS tests cover classification, thresholds, cooldown/replay, concurrency, budget-before-I/O, ambiguous delivery, threading, UTC-day rollover, absolute expiry, off switch, bounded reads, authorization and dev refusal. Python tests cover finite calendar/no retries, preflight ordering, credential redaction and immutable expiring grants. Full site suite: 1,928 passed, one skipped; Python monitor/token tests: 16 passed. Production D1 migration `0043_session_monitor.sql` was the sole pending migration and applied successfully, adding only the singleton table. The live narrow credential received 403 from `/api/me`, `/api/session-log/sessions` and `/api/db/admin_edits`. `/api/db` itself legitimately lists only tables allowed by the identity's scopes, so a 200 there is not access to table contents. Production `/admin/sessions` was reloaded and rendered successfully, with no console warnings/errors and only the three sessions of the logging-switch administrator.

[admin sessions]: https://gcs.oa.dev/admin/sessions
