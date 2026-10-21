# Usage metrics: who drives load and spend

Status: proposed 2026-10-21 (gcs; shared code, so `[cloud]`).

## Why

We have no view of what drives a deployment's Cloudflare load and bill. Today the only signals are Cloudflare's dashboard totals and our own scan costs. There is no way to answer:
- "the sheet-sync token is half our R2 Class B ops"
- "the agent API's `/api/subtree` drives the D1 rows read"
- "warm-cache after each scan costs $X/mo"

Requests come from browsers (sessions), share links, agent and CLI tokens, the job's grants (snapshot job, sheet sync, session monitor), crawlers and anonymous OG fetches.

## Two sources

1. **Cloudflare's billed totals**, via the GraphQL Analytics API, per account and day:

   | Product | Dataset | Measures | By |
   |---|---|---|---|
   | Pages Functions | `pagesFunctionsInvocationsAdaptiveGroups` | requests, errors, CPU time | script, status |
   | R2 | `r2OperationsAdaptiveGroups` | Class A/B ops | bucket, action |
   | R2 | `r2StorageAdaptiveGroups` | storage | bucket |
   | D1 | `d1AnalyticsAdaptiveGroups` | rows read/written, queries | database |
   | KV | `kvOperationsAdaptiveGroups`, `kvStorageAdaptiveGroups` | ops, storage | namespace |

   These are the units the bill uses. They have no notion of caller or route.
2. **Our attribution**, through Workers Analytics Engine: one data point per Functions request, written by the root middleware (`site/functions/_middleware.ts` `onRequest`, after the response, via `waitUntil`). One dataset per deployment (binding `USAGE`, dataset `disky_usage_<deployment>`).

   | Field | Content |
   |---|---|
   | `blob1` route | normalized pattern, e.g. `/api/subtree`, `/data/*`, `/assets/*`, `page`; never raw paths (cardinality, privacy) |
   | `blob2` method | |
   | `blob3` via | `session` / `grant` / `token` / `public` / `bot` |
   | `blob4` caller | the grant or token's **name or note**, the job grants by name, `session` for any signed-in browser. Never an email: the admin page joins to identities where it's allowed to show them |
   | `blob5` status class | `2xx`, `3xx`, `4xx`, `5xx` |
   | `blob6` cache | `edge-hit` / `kv-hit` / `miss` / `none` |
   | `blob7` store | `primary` / a `STORES_JSON` key |
   | `double1` | 1 (the count) |
   | `double2` | wall ms |
   | `double3` | R2 GETs made |
   | `double4` | R2 bytes read |
   | `double5` | D1 queries |
   | `double6` | D1 rows read, where the D1 result's `meta` gives it |
   | `double7` | KV reads |
   | index | `caller` |

   - **How the counters are filled:** routes report their caller and counts on a request-scoped meter, e.g. `meter(ctx)`, created in the middleware and threaded through `ctx`. The R2 and D1 seams that already exist bump it: the `INDEX_R2` wrappers in `_lib/index.ts`, the D1 helpers, and `edgeCache.ts` for the cache field. A route that reports nothing still gets a point, with `via` = what the gate identified, or `public`.
   - **Cost:** included in Workers Paid up to 10M data points/month (gcs is far below that). Points are sampled at high volume, so queries use `_sample_interval` (`SUM(_sample_interval * double3)`).

## Surfaces

- **`dt-cloud cf-usage [-d DAYS] [-j]`.** Pulls (1) for the account, filtered to the deployment's script/buckets/database/namespaces from its wrangler config, and (2) through the Analytics Engine SQL API. Prints a per-day table: billed units and list-price $, then the attribution by caller and by route.
  - Needs a CF API token with Account Analytics: Read. Use the existing `CLOUDFLARE_API_TOKEN` if its permissions cover it; otherwise a new read-only token, which is a secret and goes through the user.
  - Prices live in one table in code: R2 Class A/B, storage, D1 rows read/written, Functions requests/CPU, KV.
- **The daily job** runs it after `digest` and stores `snapshots/usage/<date>.json`, so `/admin/usage` reads a file, not a live API call per view. The same token is needed as a job secret.
- **`/admin/usage`** (admin):
  - totals vs the plan's included amounts and the overage $;
  - stacked bars by day: requests, R2 ops, D1 rows read, by caller (top N + other);
  - a route × caller table for a chosen window.
- **Instrumentation-only first:** phase 1 can ship before the token exists. The data points start accumulating, and Analytics Engine keeps 3 months.

## Phases

1. Middleware meter + Analytics Engine write + the `USAGE` binding (gcs `wrangler.toml`, prod and preview). Unit tests for the route normalizer, the caller resolution (session / grant name / token / public / bot) and the point shape. Deploy; verify points arrive with the SQL API, or on the CF dashboard if there is no token yet.
2. `dt-cloud cf-usage` (GraphQL + Analytics Engine SQL), with a fixture-tested parser and the price table.
3. The job step + `snapshots/usage/`, then `/admin/usage`.

## Parity

Shared code, so every deployment gets it unasked. Each deployment adds its own binding, dataset name and token, and the deployment specs list the binding as a gap until they do.
