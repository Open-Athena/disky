# Root upstream review queue

Peer handoffs received 2026-10-13; these are review candidates, not verified root implementations or authorization for infrastructure changes. Current root priority is the project-owned/public federated-scan design in `federated-scans.md`. No whole deployment/laptop merge should replace a reviewed shared-code pick.

## CW

CW reports isolated capture, serving, append/compaction and a post-revocation smoke successful through `779dc5c5`; six obsolete access grants removed without deleting stored data or credentials. Deployment-specific isolation stays on CW.

Shared candidates: `0d87420c` (`Secrets(grant_only=...)` selection helper and exact mocks), `08dad10f` (generic static OG fallback renderer), `ee4a6a05` (deployment-policy-neutral PreviewLinks/sharePreview wording and SSR test). Read/review each diff before picking; do not infer approval for Share UX redesign from the discussion. CW detailed-preview/revocation browser verification and promotion remain its owner's gate.

## Local / m3

m3 reports cloud `c56a1525` → local `7c2da9ba` → m3 `86a5d59b`, deployed through `0bd9841c`, with private configuration retained and remote CI successful. Root has not independently rerun those checks.

Reviewed root candidates from m3: `a96496b4` help focus; `5671c86a` path-hidden guests/raw-route denial; `3109d771` Hide UI; `ef17b432` hint; `eba1ca5b` + `9653a8c3` largest API/UI; `0483510c` largest path scopes; `d5612dd5` guest-key-before-routing/path encoding; `5f2fc258` VM/physical disk diff; `2b8a325b` local timestamp formatter; `51ff1c25` palette URL discoverability; `642a7eec` scan-run/health viewer-local timestamps (formatter dependency). Adapt to root's current `scanArg`/`scanSlug`; preserve newly blocked unfilterable name-summary/static-bench/filter-cover/health/scan-runs routes from m3 `86a5d59b` when porting guest privacy. Do not take deployment config as shared code.

Take only `scripts/branch-audit` from local integration `4615459c`, not that entire laptop merge. Inspect `wt/m3/specs/branch-sync-2026-10-13.md` for the classified queue. App/local and GCS/cloud have substantial reported drift; incoming/outgoing commit counts are audit snapshots, not an approved cherry-pick list. Other sessions own those active worktrees.
