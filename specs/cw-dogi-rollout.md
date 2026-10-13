# CW dynamic OpenGraph rollout

Ryan wants dynamic OpenGraph images on CW, with the reusable implementation owned by the shared `cloud` base. The renderer, HTML stamping, signed preview credentials and Share dialog already exist in the merged base; do not build another renderer here.

## October 13 handoff

The infrastructure cutover's scheduled primary scan and background compaction have passed; the OG rollout can now proceed independently of migration verification. Shared `cloud` remains unmerged. Required privacy/color checkpoints include `e2265223`, `03793a97` and the complete `500e55d4` commit (including the DOM-free core `parseColor` export); resvg needs integer RGB rather than fractional HSL. Later shared header/shortcut changes are `1211feaa` plus the mandatory `/og` catch-all routing follow-up `c56a1525`, renderer revision `r=5`. Preserve CW's strict anonymous generic policy and deployment-specific configuration; do not copy the public R2 deployment's name, scopes or public-label policy. Public R2 rollout results are peer evidence, not CW preview/production verification. No source checkpoint, merge or OG deployment has been performed here.

## Observed state — 2026-10-12

- CW production and preview configuration omit `OG_CARDS`. The HTML advertises `/og.jpg`, a stale GCS-branded static card. `twitter:title` in `site/index.html` also still names GCS.
- A public, unauthenticated fetch of `/og/map.png` already returns a dynamically rendered CW card. `OG_CARDS` gates HTML stamping, not the image endpoint.
- Anonymous cards omit tile labels and named owner legends but still display byte and object totals. Arbitrary view parameters can select a path or lens; hiding labels alone does not satisfy CW's policy that sizes stay behind site auth.
- `site/migrations/cw/0010_og_tokens.sql` exists. Its presence in Git does not establish that production D1 has applied it.
- The shared implementation is documented in [the completed dOGI spec]. Live diagnostic responses are saved privately under `tmp/og-audit/`.

## Upstream ownership

Ask `cloud` to implement and test a deployment-level anonymous-preview policy. CW needs cards with no absolute sizes, object/people counts, owner identities or data-derived existence/error details. Prefer an anonymous generic branded card for arbitrary deep links, without reading protected view data; retain dynamic detailed cards only for valid, explicitly shared credentials. Preserve another deployment's intentionally approved anonymous policy rather than silently changing GCS.

The policy must cover direct image fetches, HTML metadata, expired/revoked/invalid credential downgrades and cache hits. Include the policy/render version in cache identity; existing cached anonymous images must not bypass stricter settings. Clarify the feature flag's contract rather than implying that disabling HTML stamping disables direct card routes. Keep any internally used signed staged-plan cards in mind when defining endpoint gating.

Version unpinned latest-scan image URLs with the resolved scan identity so newly crawled links cannot retrieve yesterday's cached card under the same image URL. Detailed previews must describe the view actually rendered; do not claim a diff when the renderer only draws its after scan.

Use the existing server-side renderer, shared data access, credential checks and Share dialog. No new CW-specific rendering fork or auth bypass.

## CW ownership after upstream delivery

1. Merge `refs/heads/cloud` without rebasing, preserving existing CW changes.
2. Correct CW's static metadata/fallback branding; `/og.jpg` remains a safe fallback, not a copied GCS screenshot.
3. Read-only check that the CW `og_tokens` migration is applied; apply only a missing migration as an explicit rollout step.
4. Opt CW preview into dynamic metadata with the strict anonymous policy. Check HTML as a crawler and render the resulting image, plus the Share UI in Chrome.
5. Verify plain root/deep links disclose no protected data. Verify valid detailed shares show the intended view, and invalid/expired/revoked shares downgrade safely, including warmed-cache cases. Verify image URLs change after a new scan.
6. Deploy production only after preview verification and approval of the resulting public-card policy. Explain that already-cached Slack previews may need a fresh link fetch; deployment cannot retract a previously copied preview.

No production configuration, migration or deployment has been changed for this request yet.

[the completed dOGI spec]: done/dogi.md
