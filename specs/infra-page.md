# `/admin/infra`: what a deployment runs on, and how full it is

Status: proposed (2026-10-14). `[cloud]` feature: generic code, each deployment lists its own resources in config. No gcs defaults in shared code.

## Why

A deployment spans two clouds, and nothing on the site shows what it is built on or how close any part is to a limit. The first example is the gcs D1 (`oa-gcs-usage-auth`): about 6.3 GB of Cloudflare's 10 GB per-database cap, which only showed up by chance in a `wrangler` result. Others are R2/GCS growth, which image the daily job runs, whether the scheduler is live, and which build is on prod and on dev. `/health` covers the append-only stores' coverage and freshness. This page covers the resources underneath them.

## Page

`/admin/infra` (admin only, linked from `/admin`). One section per provider. Each row shows a resource, its live status, size or usage against its limit (a bar, amber at 80 %, red at 95 %), when that figure was read, and a link to the provider console or `/health`.

- **Cloudflare**
  - Pages project: the prod and preview (`dev`) deployments, each with its build SHA, deploy time and a link to the commit. The site's own build stamp gives the current one.
  - D1 (each binding): total size against the 10 GB cap, the last migration applied against the lineage's newest, and row counts plus approximate bytes for the largest tables. D1 refuses `dbstat`, so size each listed table with `sum(length(<blob cols>))`; the config names the tables and columns.
  - R2 buckets: bytes and object count. Free and paid limits aren't per bucket, so show growth: today and 7 and 30 days ago.
  - KV namespaces: key count, if cheap.
- **GCP**
  - Buckets the deployment writes (e.g. `oa-gcs-usage-dvx`): bytes and object count, from the deployment's own latest scan where it lists that bucket, otherwise the latest sample below.
  - Batch: the running and last 10 jobs per component label (`scan`, `interval-store`, `static-names`, …), with state, duration and a link to `/scans`.
  - Cloud Scheduler: each job's schedule, state and last attempt status.
  - Artifact Registry: the image the scheduler runs (`:latest`), its digest, build time and source SHA, plus other tags in use (e.g. `:cw`).
- **IaC**: the Pulumi stacks (`infra/gcp`, the CF stack), their last `up` time and resource counts, and any drift a `pulumi preview` reported at sampling.

## Data path

Most of this needs credentials a Pages Function doesn't have (GCP; Cloudflare account APIs) and shouldn't hold. So it splits:

1. **Samples, per scan run** (not per day): rows in D1 `infra_samples (run_id, resource, kind, used, lim, unit, status, at, detail)` (`detail` a small JSON for nesting; `run_id` the `scan_runs` row it was taken by), written by the scan job's final phase (it already has GCP credentials) plus a read-only Cloudflare token (`Account Analytics:Read`, `D1:Read`, `Pages:Read`, `R2:Read`) kept in Secret Manager. It goes to the deployment's R2 (`INDEX_R2`, e.g. `infra/<date>.json` + `infra/latest.json`) and is kept 30 days, which gives the growth columns.
   - CF figures: GraphQL analytics `r2StorageAdaptiveGroups` for R2, D1 `GET /accounts/:id/d1/database/:id` (`file_size`) for D1, Pages deployments list.
   - GCP figures: `gcloud batch/scheduler/artifacts` JSON, and bucket sizes from Cloud Monitoring `storage/total_bytes`.
   - A `dt-cloud infra-sample` CLI takes the samples (`-n` prints them as JSON without writing), so it can also run ad hoc.
2. **Live** (read by the function on request, cheap): D1's own size (the `size_after` field D1 returns on a trivial read), migration state (`d1_migrations`), the configured tables' row counts and bytes, and the site's build stamp. Live values replace the latest sample for the same resource.
3. `GET /api/infra` merges the two (admin-gated, colo-cached about 60 s like `/api/health`).

Config: the deployment's wrangler vars (or a small `infra.yml` in the deployment branch) list the resources to show and their limits. Shared code knows only resource kinds, never gcs names.

## Alerts (phase 2)

The session-monitor pattern applies: a quiet checker posts to the deployment's admin channel when any resource crosses 80 %. One alert per resource per threshold, with a cooldown. The D1 cap is the first such limit.

## Phases

1. Live D1 section + `/api/infra` + page skeleton. No new credentials. This alone surfaces the D1 cap.
2. `infra-sample` CLI + scan-job phase + `infra_samples` migration + read-only CF token (needs the user's go: new secret, IAM) → GCP, R2, Pages and IaC sections.
3. Threshold alerts.

## Open questions

- What is filling the gcs D1? The likely candidates are index footers (`index_row_groups`, hot 30 scans), `session_log`, and owner prefix caches (≈21 MB). Phase 1's table sizing answers it. Whether to tighten `INDEX_RETAIN` or move footers out is a separate decision.
- `owner_totals` is unused since a92e0165. Drop it with a migration (frees its rows) once that commit is live.
