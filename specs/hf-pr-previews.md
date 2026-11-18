# hf PR previews

A PR into `hf` gets a preview (`full` mode, [`cf-pr-previews.md`]): front-door's `pr-preview.yml` reads `hf`'s `.github/cf-preview.json` (Pages project `oa-hf-usage`), publishes branch `pr-N` to `pr-N.oa-hf-usage.pages.dev` in the `hf-preview` environment, and retires it on close. Sign-in there is OA only, through the broker's `oa-hf-usage-preview` client; no silent attempt.

Pilot 2 (2026-11-18): create, update on a new head, sign-in, and retire on close through the front-door caller (pilot 1, PR #3 on 2026-11-12, ran through the retired `hf-preview.yml`).

[`cf-pr-previews.md`]: cf-pr-previews.md

Create: run 44296024236 published `73eb66ee3` to `pr-8.oa-hf-usage.pages.dev` (the probe read its `x-disky-build`).
