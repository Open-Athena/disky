# Browser fetch-failure diagnostics

Status: R2 implemented and deployed to dev/prod on November 10 UTC. The shared auth source patch is ready for upstream review; other deployments retain their own rollout ownership.

Requested November 9 after the `f0a93778` session's two status-zero fetch failures at 23:04 UTC. Stored events show successful other requests at the same time, and both affected endpoints returned 200 at the next recorded refocus at 23:58. Independent endpoint checks also passed. A wake/reconnect is plausible, not established; no DNS, TLS or server cause can be recovered from the browser's generic error alone.

## Shared implementation

Adopt root's reviewed session-log extraction (`1ccf5b0c`, `517a8d20`) and extend its existing reproducible auth-package patch, rather than restoring an app-owned logger. The accompanying TypeScript source patch targets auth source `22b5eae`; the immutable dist pin and frozen pnpm lock remain the installation contract. Other deployments can review these shared changes independently of R2's configuration and live schema apply.

- `life` records initial online/visibility hints, prior tab discard where exposed, online/offline, freeze/resume, pageshow/pagehide and BFCache persistence. A ten-second pulse records gaps longer than thirty seconds; these may be background throttling, suspension or a busy main thread, not necessarily sleep. The pulse is removed when logging stops.
- Each fetch gets a page-local request number, start/end online and visibility hints, time since becoming visible, recent scheduling-gap context, end timestamp, wall duration and the existing monotonic duration. Visibility is observed at fetch start too, so query-library listeners running first do not leave stale context.
- Responses retain their type and a syntactically valid Cloudflare Ray ID when available. A rejected fetch has no response and cannot supply a Ray ID. No extra request headers or diagnostic requests are added.
- Cancellation records whether an explicit signal, AbortError or pagehide supplied the evidence. Arbitrary abort reasons, cookies, authorization and header collections are not copied. Response diagnostics keep only the valid Ray ID and existing cache/engine fields. Existing URL masking and body summaries remain in effect.
- The session timeline identifies a later successful repeat only for an exact unmasked same-origin GET/HEAD on the same page, started after the failed request ended. Concurrent earlier reads, different queries, page reloads and writes do not qualify. The original error remains counted; a later success is evidence about that later attempt, not proof of the original cause.

The shared wire whitelist accepts only the declared scalar fields. Lifecycle events follow the existing runtime logging switch and per-kind configuration; their activity-feed notification defaults off. Existing failed-attempt notifications include the new compact context. This adds no independent raw-log retention or permission changes.

## Verification and rollout

Public-boot client regressions cover connectivity changing in flight, a 48-minute scheduling gap, whitelist round-trip, signal abort privacy, Ray-header filtering and visibility-handler ordering. Model regressions cover truthful later-success links and exclusions. The Chromium fixture renders the real shared switch/list/timeline and diagnostics with synthetic responses. Heavy suites run in CI. Chrome MCP and the usual dev node were unavailable at the initial check; do not claim CIC from API checks alone.

The extracted store's nullable actor columns were missing in R2. On November 10 UTC, its two-column recipe was applied once and removed. Readback found exactly `grant_id` and `subject_name` added; the indefinite logging switch was unchanged, and the original session continued from 131 to 135 events with its original two errors. Evidence: `tmp/session-diagnostics-schema-proof.json`. Other deployment owners must check and apply their own schema; this is not an authorization for their rollout.

All three isolated local Chrome checks passed; the new screenshot was visually inspected. The usual dev node timed out, and Chrome MCP remained busy, so these are Playwright checks in a separate Chrome instance, not MCP CIC. Local source compilation and site TypeScript checks passed. Initial CI caught an omitted model export in the replacement package patch; its model export and anonymous-read retention guard were restored before any app deployment. The public boot tests also needed the package's explicit reset between cases, because external package module state survives Vitest's module reset.

Final runtime `35d57919120ed1feab4bc58b6e55b9df8bde6f94` passed full CI `42728468479`, including 4,358 site tests (two skipped). It deployed from the clean isolated checkout to dev `c167f8c0`, then prod `ea91cffb`. Both entry-asset gates passed. Identity, `series?path=marin-na` and logging-config reads returned 200 on both environments, with exact dev/prod JSON equality. Cloudflare confirmed the production source revision. Evidence: `tmp/session-diagnostics-{dev,prod}-deploy.log`, `tmp/session-diagnostics-{dev,prod}-probe.json`, `tmp/session-diagnostics-cf-status.log`, `tmp/session-diagnostics-green-site-ci.log`.

Synthetic browser batches stayed in the fixture; no fake production activity or Slack post was created for verification. The runtime switch stayed unchanged. New page loads capture the added context; older events are not enriched retroactively. No CS3 retention policy or receipt transport was enabled by this rollout.
