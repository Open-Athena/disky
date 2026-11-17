# cw PR previews

cw runs `full`-mode PR previews (specs/cf-pr-previews.md) since 2026-11-17: a PR into `cw` from a write+ author (or a head a writer approves with `/preview <full-sha>`) deploys as branch `pr-N` of the Pages project `oa-cw-s3-usage`, at `https://pr-N.oa-cw-s3-usage.pages.dev`, on the dev stack's preview environment (production D1/R2, as `dev.cw.oa.dev`). Sign-in there is the broker's preview client `oa-cw-s3-usage-preview` or an emailed code.

Pieces: `.github/cf-preview.json` (target `cw`), `PAGES_SUBDOMAIN` in both `site/wrangler.toml` env blocks, the GitHub environment `cw-preview` (branch policy `front-door`; secret `CF_PAGES_DEPLOY_TOKEN`, a Pages Edit token for previews only; var `CLOUDFLARE_ACCOUNT_ID`), and `cw` in front-door's `pr-preview.yml`.
