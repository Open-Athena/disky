# Browser fetch-failure diagnostics

Requested November 9 after the `f0a93778` session's two status-zero fetch failures at 23:04 UTC. Stored events show successful other requests at the same time, and both affected endpoints returned 200 at the next recorded refocus at 23:58. Independent endpoint checks also passed. A wake/reconnect is plausible, not established; no DNS, TLS or server cause can be recovered from the browser's generic error alone.

## Shared implementation

Adopt root's reviewed session-log extraction (`1ccf5b0c`, `517a8d20`) and extend its existing reproducible auth-package patch, rather than restoring an app-owned logger. The accompanying TypeScript source patch targets auth source `22b5eae`; the immutable dist pin and frozen pnpm lock remain the installation contract. Other deployments can review these shared changes independently of R2's configuration and live schema apply.

- `life` records initial online/visibility hints, prior tab discard where exposed, online/offline, freeze/resume, pageshow/pagehide and BFCache persistence. A ten-second pulse records gaps longer than thirty seconds; these may be background throttling, suspension or a busy main thread, not necessarily sleep. The pulse is removed when logging stops.
- Each fetch gets a page-local request number, start/end online and visibility hints, time since becoming visible, recent scheduling-gap context, end timestamp, wall duration and the existing monotonic duration. Visibility is observed at fetch start too, so query-library listeners running first do not leave stale context.
- Responses retain their type and a syntactically valid Cloudflare Ray ID when available. A rejected fetch has no response and cannot supply a Ray ID. No extra request headers or diagnostic requests are added.
- Cancellation records whether an explicit signal, AbortError or pagehide supplied the evidence. Arbitrary abort reasons, cookies, authorization and response headers are not copied. Existing URL masking and body summaries remain in effect.
- The session timeline identifies a later successful repeat only for an exact unmasked same-origin GET/HEAD on the same page, started after the failed request ended. Concurrent earlier reads, different queries, page reloads and writes do not qualify. The original error remains counted; a later success is evidence about that later attempt, not proof of the original cause.

The shared wire whitelist accepts only the declared scalar fields. Lifecycle events follow the existing runtime logging switch and per-kind configuration; their activity-feed notification defaults off. Existing failed-attempt notifications include the new compact context. This adds no independent raw-log retention or permission changes.

## Verification and rollout

Public-boot client regressions cover connectivity changing in flight, a 48-minute scheduling gap, whitelist round-trip, signal abort privacy, Ray-header filtering and visibility-handler ordering. Model regressions cover truthful later-success links and exclusions. The Chromium fixture renders the real shared switch/list/timeline and diagnostics with synthetic responses. Heavy suites run in CI. Chrome MCP and the usual dev node were unavailable at the initial check; do not claim CIC from API checks alone.

The extracted store needs the nullable actor columns in `site/migrations/session-log-actor.sql`. Check the live schema first, apply only if missing, and preserve the runtime switch's current state. Deploy only from a clean CI-passed checkout, dev before prod. Confirm endpoint health, preserved session history and the tested client/server wire round-trip before calling the rollout complete. Synthetic browser batches stay in the fixture; do not create fake production activity or Slack posts as a verification step.
