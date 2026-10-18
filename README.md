# disky @ Open Athena

**This branch is only an index. The code lives on the deploy branches below.**

[disky] maps where storage goes: scheduled scans of a store's buckets, then a treemap site with sizes over time, diffs between scans, owners and read recency. Open Athena runs four deployments, one per storage provider. Each has its own branch in this repo, and each branch merges the shared base, [`runsascoded/disky@cloud`][cloud].

| branch | site | scans |
|---|---|---|
| [`gcs`] | [gcs.oa.dev] | Marin's GCS buckets (`marin-*`), with per-user ownership, read recency from access logs, and staged deletion |
| [`cw`] | [cw.oa.dev] | Marin's CoreWeave (CAIOS) S3 buckets, with zone quotas and lifecycle rules |
| [`hf`] | [hf.oa.dev] | the `open-athena` Hugging Face org: its buckets, models, datasets and spaces, including the history its storage bill counts |
| [`r2`] | [r2.oa.dev] | Open Athena's Cloudflare R2 buckets, including disky's own indexes |

The sites are Open-Athena-gated. Each branch's README says how its scan runs, what it changes from `cloud`, and where its infrastructure lives.

## What's on this branch

- **`.github/workflows/health.yml`**: the hourly serving check across all four sites. It lives here because GitHub runs `schedule:` workflows only from a repo's default branch.
- **A stub for `CI`**: GitHub dispatches a workflow by hand only once a file of that name exists on the default branch. The stub has the real workflow's `name:`, `on: workflow_dispatch` only, and one step that prints where the real file is; dispatching it with a deploy branch as the ref runs that branch's real file. CI itself runs on each deploy branch's pushes.

## Elsewhere

- **The shared base:** [`runsascoded/disky`][upstream], branch [`cloud`][cloud]: the scanning engine, the `dt-cloud` overlay, the site, and the specs.
- **The public demo:** [r2.rbw.sh], built from the same base.

[disky]: https://github.com/runsascoded/disky
[upstream]: https://github.com/runsascoded/disky
[cloud]: https://github.com/runsascoded/disky/tree/cloud
[`gcs`]: https://github.com/Open-Athena/disky/tree/gcs
[`cw`]: https://github.com/Open-Athena/disky/tree/cw
[`hf`]: https://github.com/Open-Athena/disky/tree/hf
[`r2`]: https://github.com/Open-Athena/disky/tree/r2
[gcs.oa.dev]: https://gcs.oa.dev
[cw.oa.dev]: https://cw.oa.dev
[hf.oa.dev]: https://hf.oa.dev
[r2.oa.dev]: https://r2.oa.dev
[r2.rbw.sh]: https://r2.rbw.sh
