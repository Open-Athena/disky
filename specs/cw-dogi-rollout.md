# CW dynamic OpenGraph rollout

Ryan wants dynamic OpenGraph images on CW, with the reusable implementation owned by the shared `cloud` base. The renderer, HTML stamping, signed preview credentials and Share dialog already exist in the merged base; do not build another renderer here.

## October 13 handoff

The infrastructure cutover's scheduled primary scan and background compaction have passed. Ryan authorized committing the migration, merging upstream and rolling out dOGIs. Migration checkpoint `1e47685c` and merge `f20e5c0f` retain CW-owned configuration; `cloud` through `362ca1a6` is merged without rebasing. Required privacy/color checkpoints include `e2265223`, `03793a97` and the complete `500e55d4` commit (including the DOM-free core `parseColor` export); resvg needs integer RGB rather than fractional HSL. Shared header/shortcut changes include `1211feaa` plus the mandatory `/og` catch-all routing follow-up `c56a1525`, renderer revision `r=5`. CW's explicit policy is `OG_CARDS=1`, `OG_ANONYMOUS=generic`, `OG_DEFAULT_COLOR=t`, with no `PUBLIC_READ`. Public R2 configuration and label policy were not copied. Public R2 rollout results are peer evidence, not CW deployment verification.

The read-only `job/cw-dogi.py schema` check confirmed applied migration `0010_og_tokens.sql` and table `og_tokens`; no migration apply is needed. Static Twitter branding is corrected, and `/og.jpg` is generated from the shared generic card renderer, without protected data. Shared commit `08dad10f` adds `site/scripts/render-og-fallback.mjs`, rendering a named generic PNG without a network listener; JPEG conversion uses the project's existing `sips` convention. Merged validation passed all 1,944 site tests (one skip) and both typechecks/builds. CW configuration/manual-launcher and verification-helper tests pass (18 cases). Shared `ee4a6a05` corrects admin copy to describe anonymous cards by deployment policy; three exact render/share URL tests pass.

Preview deployment `7610564c` enables this policy. `job/cw-dogi.py verify -o tmp/cw-dogi-preview-anonymous-checks preview` passes six exact generic HTML metadata checks and seven byte-identical generic PNG checks, including absent/deep/staged/user/assignment and invalid-signature requests. The OA browser renders the intended CW root view and Share dialog. Final preview deployment `433d53f5` uses application source at `ee4a6a05`; its build passes both typechecks and the browser confirms the corrected admin copy. Production has not been promoted: a single temporary detailed-preview link must still be verified and immediately revoked, including its warmed image downgrade. Browser minting awaits Ryan's action-time confirmation; no browser token has been minted. Ryan noticed the dialog and asked about its relationship to recipient invitations: both existing options have detailed previews; preview-only does not grant dashboard access, while the bearer access link grants the CW read scope without collecting recipient attribution. Do not silently treat that question as approval to mint either kind or redesign the shared flow.

The CW job grant is intentionally read-only: the CLI's `--detailed` mint attempt correctly returned HTTP 403 without creating a token. Do not widen it to make a test pass. Use the signed-in authorized browser for this verification, keeping the capability URL in memory, never posting it, and revoking only the newly minted test row. The CLI's anonymous verification remains usable with no viewer grant. Its current detailed mode reads the default job grant and therefore cannot complete CW's mint test; adding an explicit mint-capable operator credential input would be a separate helper improvement. Unit tests cover actual expired/revoked credentials and private/no-store detailed responses; those are not a substitute for CW live detailed verification.

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

Production deployment has not changed for this request yet; the source configuration is prepared for the reviewed rollout.

[the completed dOGI spec]: done/dogi.md
