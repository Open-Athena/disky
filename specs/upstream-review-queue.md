# Root upstream review queue

Peer handoffs received 2026-10-13. Useful in-scope work is reviewed and adopted proactively; this queue is not a substitute for implementation. No whole deployment/laptop merge should replace a reviewed shared-code pick, and no handoff authorizes unrelated infrastructure changes.

## Root adoption checkpoints

- `af65a317`: snapshot-proxy store scope (`022359cd` + `d6c25a38`), with seven regressions reproduced before the fix and 23 scope/store tests passing afterward. Direct file-proxy prefix configuration remains a deployment-owner check.
- `c48c477d`: parent-aware `scripts/branch-audit` from `4615459c`; local→cloud, m3/app→local. Syntax and real m3/local audit checked.
- `2d7478c7`: package-local Vitest/jest-dom setup repair, required for meaningful widget validation without dependency upgrades.
- `6e22366e`: atomic corrected title-space geometry (`ca4aea08` + `3f917f18`), retaining the complete final spec from `9b42a0e2`; `c5d8dddf`: shared controls and parent frames (`5621357b`). The option remains disabled by default.
- `a002671c`: consumer settings, mixed bucket URI labels, qualified titles, job/run links, and actual scan-availability events (`3e0978a6`).
- `83a41c38`: sticky diff endpoint pinning (`e2cea646`); `a9616ab5`: page-search discoverability (`51ff1c25`).
- `468302ba`: shared capture timestamp formatter adapted from `2b8a325b` without its laptop deployment log; `21a5eed8`: viewer-local run timestamps (`642a7eec`).
- `9b2aab6f`: CW's section-aware diff OG (`dabc0edb`), with both endpoints pinned and signed; focused privacy/render checks passed.
- `7f6263dc`: local-only synthetic table fixture and browser regressions; `c8f952f9`: additive checkbox-cell hit targets and persistent unfiltered staging feedback (CW mail 161). Four browser regressions failed against the unfixed component, then all six table checks passed after the fix, alongside four federation checks. No production staging was performed.

Final combined validation: 2,021 site tests passed (one skipped), 181 core widget tests passed, ten browser regressions passed, core typecheck and site build passed. Chrome verified DOM/canvas title-space controls, a synthetic diff card, actual-checkbox versus adjacent-padding selection, staging progress, and success retained after selection clears. The development fixtures remain explicitly fictional, not production inventories. Root sent shared-code handoffs to CW, GCS, OA R2 and m3; GCS acknowledged ownership of its reviewed pickup/verification/rollout. All checkpoints above are local root changes, not claims of live deployment. The remaining sections retain peer provenance and outstanding candidates.

## CW

CW reports isolated capture, serving, append/compaction and a post-revocation smoke successful through `779dc5c5`; six obsolete access grants removed without deleting stored data or credentials. Deployment-specific isolation stays on CW.

Shared candidates: `0d87420c` (`Secrets(grant_only=...)` selection helper and exact mocks), `08dad10f` (generic static OG fallback renderer), `ee4a6a05` (deployment-policy-neutral PreviewLinks/sharePreview wording and SSR test). Read/review each diff before picking; do not infer approval for Share UX redesign from the discussion. CW detailed-preview/revocation browser verification and promotion remain its owner's gate.

Later handoff (mail 156/157): `e2cea646` fixes sticky diff endpoint pin modes and `dabc0edb` implements section-aware diff dOGIs (`#diff`/`#changes` → `section=diff`, distinct token-scoped diff kind, both exact scans signed, DOM-free diff renderer/color subpath, truthful comparison metadata, revision 6). Root adopted these separately as `83a41c38` and `9b2aab6f`, without CW deployment-only `98b76238`. Root's 93 focused tests and synthetic renderer/browser check passed; live exact-view token testing/prod promotion remain owner gates. No live token was minted here.

## Local / m3

m3 reports cloud `c56a1525` → local `7c2da9ba` → m3 `86a5d59b`, deployed through `0bd9841c`, with private configuration retained and remote CI successful. Root has not independently rerun those checks.

Reviewed root candidates from m3: `a96496b4` help focus; `5671c86a` path-hidden guests/raw-route denial; `3109d771` Hide UI; `ef17b432` hint; `eba1ca5b` + `9653a8c3` largest API/UI; `0483510c` largest path scopes; `d5612dd5` guest-key-before-routing/path encoding; `5f2fc258` VM/physical disk diff; `2b8a325b` local timestamp formatter; `51ff1c25` palette URL discoverability; `642a7eec` scan-run/health viewer-local timestamps (formatter dependency). Adapt to root's current `scanArg`/`scanSlug`; preserve newly blocked unfilterable name-summary/static-bench/filter-cover/health/scan-runs routes from m3 `86a5d59b` when porting guest privacy. Do not take deployment config as shared code.

Root adopted only `scripts/branch-audit` from local integration `4615459c` as `c48c477d`, not that entire laptop merge. Inspect `wt/m3/specs/branch-sync-2026-10-13.md` for the classified queue. App/local and GCS/cloud have substantial reported drift; incoming/outgoing commit counts are audit snapshots, not an approved cherry-pick list. Other sessions own those active worktrees.

m3's run-history recommendation: share Run/Phase/Output records, sink/upsert, measurement, API and UI; retain concrete GCP/AWS job mapping/log/console modules and small deployment lifecycle hooks. Local captures emit the same records without fake Batch jobs. Capture scan identity is separate from execution/attempt/parent identity. Provider-specific details and unavailable historical phases/cost estimates remain optional and labelled. This is peer architectural context, not an implemented root change or mandate for a generic execution abstraction.

Root `$read m3` completed 2026-10-13: resolved Codex `01a10c2a-55d4-7300-be39-7128076c1eee` at `/Users/ryan/c/disky/wt/m3`; normalized extraction covers raw lines 4118–4716, including compaction context, through `2026-10-13T17:51:33.163Z`. Bilateral verdict was `BOTH`, then `THEM` after advancing: root was caught up; m3 had not read root's newer context at that checkpoint. Read the active `wt/m3/specs/branch-sync-2026-10-13.md` and shared `scan-runs-ui.md` in full. Parent integration/rollout and both CI runs were reported complete; AWS recording/backfill is still not implemented, so an empty `/scans` does not mean missing snapshots. The code-sharing-boundary ask was answered by the narrow run-history decision above, not a blanket instruction to merge outgoing commits or change infrastructure. The parent-aware audit, palette and timestamp handoffs are now adopted and listed above.

This is compatible with federated scan catalogs: a catalog pins completed data observations; run history tracks capture/index attempts and their phases. Share the scan identity between them without treating a job submission as a completed export or forcing project-owned schedulers into one executor API.

## OA R2 title-space correction

Root adopted the original `ca4aea08` and correction `3f917f18` atomically as `6e22366e`, with the complete final `specs/done/treemap-title-space.md` from `9b42a0e2`. Exact proof: all direct fixture siblings share multiplier `25/23`, not one unchanged column and one vertically stretched column. Root independently verified 181 core tests, build/types and DOM/canvas rendering in Chrome. R2's own dev/prod rollout and CI remain owner-reported. Public opt-in stays a separate deployment decision; no flag was enabled here.

Root adopted `5621357b` as `c5d8dddf` (controlled title-space toggle and optional ancestor frames), and `3e0978a6` as `a002671c` (consumer controls, mixed bucket URI labels, qualified run/health titles, clickable job/run links and actual observation markers). Defaults remain unchanged and no OA R2 profile was taken as library code.

Root adopted `022359cd` + `d6c25a38` as `af65a317`: snapshot-proxy namespaces are restricted, and regressions live under non-routed `functions/_lib/dataProxy.test.ts`. Seven pre-fix tests reproduced scope leakage. The fixed proxy uses `snapshotsPrefix` even for the primary, validates a canonical first relative scan-ID segment, and deliberately retains primary `rules.json`; 23 scope/store tests passed. Deployment owners must separately narrow direct file-proxy prefixes where needed; shared `/data` code alone does not tighten deployment `STORE_PREFIXES`. No root deployment change or live exploit check occurred.

R2 owner reports final `0af2809a` live on its own dev/prod, both full CI runs green, and the scheduler scan publishing primary/meta outputs successfully. Catalog source is `/Users/ryan/c/oa/oa-index` (`Open-Athena/oa-index`); its owner registered the R2 entry through the form. These are owner reports, not root-run verification or root catalog changes.
