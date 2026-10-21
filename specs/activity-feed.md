# Activity feed: who did what in the app, in /admin and the admin channel

Feedback from the 10/20 presentation: when someone acts in the app (assigns a path to a user, stages a deletion, grants access, writes a chart note) nobody hears about it. More broadly, admins want visibility into how people use the app: what they open, which controls they use, where they drill.

The session log (`specs/done/session-log.md`) already records the browser side (page loads, URL changes, control clicks, the path filter, pickers, fetches, errors) while an admin switch is on, for at most 31 days, viewable at `/admin/sessions`. This spec adds:

1. **Server-side action events**: every write an identified actor makes, browser or agent token, recorded once, server-side.
2. **Bursts**: one person's activity with no gap over 15 minutes (`BATCH_GAP_S`, the staged-deletion rule), summarized.
3. **Sinks**: each burst is one Slack thread in the deployment's admin channel and/or one Discord message, edited in place as the burst grows.
4. **Per-kind configuration** in `/admin`: for each kind of event, whether to record it and how to report it.
5. **Always-on logging**: the session-log switch gets a "no end date" setting, and `/privacy` says what is recorded while logging is on.

## Event kinds

A kind is a stable string. Server kinds come from write requests; client kinds are the session log's.

| kind | source | default record | default notify |
|---|---|---|---|
| `assign` | `POST /api/actions` (owner set or cleared, prefix or object) | on | `line` |
| `note` | `POST/PUT/DELETE /api/annotations` | on | `line` |
| `stage` | staging writes (`/api/plans/*`) | on | `count` (its own thread already lists each; the burst links to it) |
| `deletion` | `/api/sweep/*`, `/api/plan-sweep/*` (dispatch, stop, undo, purge) | on | `line` |
| `access` | `/api/auth/*` writes: grant mint / rotate / revoke, request decisions, share links | on | `line` |
| `admin` | `/api/db/*` writes (the admin console's table edits), the session-log switch, the activity config | on | `line` |
| `token` | `POST /api/token` (an agent token minted) | on | `line` |
| `profile` | profile writes (name, avatar) | on | `count` |
| `write` | any other successful non-GET by an identified actor | on | `count` |
| `view` | client `start` (a page load) | on | `count` |
| `nav` | client `nav` (any URL change: route, query params, `?section=`) | on | `count` |
| `click` | client `click` | on | `count` |
| `filter` | client `filter` | on | `count` |
| `pick` | client `pick` | on | `count` |
| `error` | client `error` / `reject` / `console`, and `api` with status 0 or ≥ 500 | on | `count` |
| `api` | client `api` (other fetches) | on | `off` |
| `prefetch`, `viewport`, `vis` | client | on | `off` |

- **record** (`log`): off drops the kind before storage. For a client kind, the server drops those events from each session-log batch. For a server kind, no event row is written.
- **notify**: `off` (not in Slack/Discord), `count` (a count in the burst's summary), `line` (a count, plus one line per event in the burst's reply for that kind).
- Session-log POSTs and pure reads sent as POST (`/api/session-log`, `/api/footer-cache`, `/api/digest-image`, `/api/static-bench`, `/api/prefixes`, sign-in flows) are never `write` events.

### Configuration: `activity_kinds`

One row per kind that differs from its default (`kind` PK, `log` 0/1, `notify` `off|count|line`, `who`, `ts`). No row = the default above. Each change appends an `admin_edits` row (`tbl = 'activity_kinds'`), as the session-log switch does. Read at most once per 60 s per isolate (the switch's `SWITCH_TTL_MS` pattern); an admin's write refreshes that isolate at once.

`/admin` → "Activity" section: a table, one row per kind (kind, what it covers, record checkbox, notify select), next to the session-log switch.

## Server events

Captured in `_middleware.ts`, after the route answers: a non-GET request that got a 2xx, from an identified actor (`identify`: an email session, a grant session or an agent token), whose route maps to a server kind. The middleware records:

- `ts`, `kind`, `actor` (email, else the grant's name, else `grant:<id>`), `via` (`session` / `grant`), `method`, `route` (the path, secret params masked as the session log masks them).
- `body`: the request body summary the session log uses for `api` events (per top-level key: an array's length, a `kind`/`op`/`action`/`mode` string, else the value's type).
- `detail`: optional, set by the route on `ctx.data.activity`: the human line and any structured fields. Routes without one get a generic line (`POST /api/annotations`).

Routes that set `detail`:
- `/api/actions` (`assign`): per action, `pattern → owner` (`→ nobody` for a clear), the kind (folder / object), the memo if any, and the prefix's size at the latest scan (`prefixesAt`, best-effort; absent if the lookup fails).
- `/api/annotations` (`note`): the chart, the note's text (≤ 120 chars) and whether it was added, edited or removed.
- `/api/token` (`token`), the `/api/auth/*` writes (`access`): what was minted / rotated / revoked / decided, and for whom (email or grant name, never a key).
- `/api/db/*` (`admin`): the table, the key and the action (`insert`/`update`/`delete`), as `admin_edits` records it.
- The sweep and plan-sweep routes (`deletion`): the run, its mode (dry run / real) and the plan.

Recording runs in `waitUntil` and is best-effort: a failure is logged and never fails the request.

## Bursts

A burst is one actor's events with no gap over `BATCH_GAP_S` (15 min). Server events and client batches from the same actor join the same burst: client batches are keyed by the session's email, or `sid:<sid>` for an anonymous viewer on a `PUBLIC_READ` deployment.

### Storage (migration `activity-feed`)

- `activity_events`: `id`, `burst_id`, `ts`, `kind`, `actor`, `via`, `method`, `route`, `line`, `detail` (JSON). Server events only; client events stay in `session_log_batches`.
- `activity_bursts`: `id`, `actor`, `via`, `first_ts`, `last_ts`, `counts` (JSON `{kind: n}`), `pages` (JSON: the most-viewed paths, top 5, from `view`/`nav`), `sids` (JSON: the session-log sessions it covers), `slack_ts` (the thread's parent), `slack_replies` (JSON `{kind: ts}`), `discord_id`, `rendered_ts`, `dirty`.
- `activity_kinds`: above.
- Retention: `SESSION_LOG_RETAIN_DAYS` (default 30) covers all three, purged by the session log's hourly purge.

## Sinks

A burst is reported once any of its events has `notify` other than `off`. Each sink is optional and is skipped when it isn't configured.

### Slack: `SLACK_BOT_TOKEN` + `SLACK_ADMIN_CHANNEL` (already set on gcs and cw)

- **Parent** (the summary, re-rendered on each update):
  - who (plain text, never a mention), how (browser / agent token), the span (`10:02–10:41Z`);
  - counts by kind (`12 assignments · 2 notes · 31 views · 58 URL changes · 40 clicks`);
  - highlights:
    - the largest assignment by bytes (`marin-us-east5/checkpoints/ (1.2 PB) → dlwh`);
    - owners affected;
    - access granted;
    - errors seen;
    - the most-viewed pages;
  - links to the burst in `/admin/activity` and its sessions in `/admin/sessions`, plus the staged plan's thread when `stage` events are in it.
- **Replies**: one per `line` kind in the burst, listing its events (≤ 30 lines, then `+N more`), edited in place while the burst continues. A new burst is a new thread.
- **Rate**: an event with notify `line` renders at once. Count-only changes render at most every 20 s per burst. If one arrives inside the window, the request waits in `waitUntil` until the window ends, re-reads the burst and renders it if it's still dirty, so the last count always lands.

### Discord: `DISCORD_ADMIN_WEBHOOK` (a secret; a channel webhook URL)

- One message per burst (`POST …?wait=true` returns its id), edited in place (`PATCH /webhooks/<id>/<token>/messages/<msg>`). Content: the same summary, then each `line` kind's list, cut to fit Discord's 2,000-character limit, with `+N more` and the `/admin/activity` link.
- Same rate rule as Slack.

### Mentions

None in this version: posts name people in plain text, as the staged threads do, and the admin channel is where people find out. Mentioning the assigner or assignee needs each person's Slack / Discord id, and some users (e.g. Stanford) are in neither. Mentions are a follow-up: a per-kind option that mentions the target when the deployment knows their id.

## Always-on session logging

- The switch's date becomes optional: "no end date" (`until_ms` = a sentinel far future, shown as "on, no end date"). Turning it on with a date keeps today's behaviour.
- While logging is on (any end date), `/privacy` carries a paragraph saying what is recorded (the event kinds above with `record` on, never form contents other than the path filter, never keys or tokens), who sees it (the deployment's admins), and for how long (`SESSION_LOG_RETAIN_DAYS`).
- The session-log `POST` also feeds the actor's burst: per batch, the counts of the client kinds whose `record` is on, the paths viewed, the `sid`.

## `/admin/activity`

Admin only. A list of bursts, newest first: actor, how, span, counts by kind, a link to the Slack thread. Expanding a burst shows its server events (time, kind, line) and links to its sessions in `/admin/sessions`. Filters: actor (substring), kind, since; kept in the URL.

## Deployments

`[cloud]` code; each deployment applies the `activity-feed` migration (`cloud`'s own D1 is the r2 demo's, which has no write actions; it gets the table, and the feed stays quiet).

| deployment | Slack | Discord |
|---|---|---|
| gcs | `#gcs-admin` (configured) | `DISCORD_ADMIN_WEBHOOK`: Ryan creates the webhook (which server / channel: open) |
| cw | `#cw-admin` (configured) | as gcs |
| hf, r2 | not configured: a gap in their deployment specs | not configured |

## Tests

- Pure parts, pinned with exact output: route → kind mapping, body summary, burst assignment (gap edge cases, client + server events joining), the summary and reply renderers for Slack and Discord (truncation, `+N more`), the kinds config merge (defaults + rows).
- `sqliteD1` for the storage: events → bursts → counts; config changes; retention.
- Sinks with a stubbed `fetch`: first event posts a parent; a `line` event posts a reply then edits it; count-only updates are throttled and the trailing render lands; a Slack failure doesn't fail the request.
