# disk-tree

Cloud storage usage analyzer: a scanning/indexing CLI (`disk-tree`), a cloud overlay (`dt-cloud`), and a Cloudflare-hosted site (`site/`) over the indexes.

## Project Vision

Track storage usage across object stores: S3, R2 and GCS buckets.

Key goals:
- **Always-ready index**: Scheduled scans (the r2 demo's daily ingest) so the index is there when you need it
- **Fast indexing**: Shard a bucket's listing across workers (`bulk-list`), aggregate out of core (`import`)
- **Site**: Treemap visualizations, diffs and an age lens over each scan's path index

## Architecture

### Python Backend (`src/disk_tree/`)

**Listing** (`find/bulk*.py`): `bulk-list` shards a bucket's object listing (`gcs://`, `s3://`,
`r2://` via its S3 endpoint) across worker processes into layer-1 listing parquet shards
(`hf://<namespace>`: a Hugging Face org's buckets + repos over the Hub API, one rate-limited process). The
canonical columns are `bucket, name, size_bytes, created, storage_class_id, generation`; generation
is the exact GCS object identity (nullable for S3/R2 and old listings), retained so a reviewed
deletion manifest can never resolve to a replacement object at the same path.

**Aggregation** (`find/import_listing.py`, `find/index.py`, `find/aggregate_{duckdb,stream}.py`):
`import` aggregates a listing bottom-up into the canonical layer-2 frame — columns `path`, `size`,
`mtime`, `kind`, `parent`, `uri`, `n_desc`, `n_children`, `n_files`, `depth` (+ opt-in pivots,
`mtime_mean`, labels), sorted `(depth, path)` so parquet pushdown prunes by depth and path prefix.
Three engines, byte-identical output: `pandas` (in memory), `duckdb` (out of core), `stream`
(O(depth) over sorted listings).

**Output** (`storage/`, `sqla/model.py`): each imported bucket's layer-2 is adopted into the write
dir (`<DISK_TREE_ROOT>/scans/<uuid>.parquet`, or `--to <dir|url>`) and recorded as a `Scan` row
(`path`, `time`, `blob`, root `size`/`n_children`/`n_desc`/`mtime`) in `<DISK_TREE_ROOT>/disk-tree.db`.
cw-s3's jobs read that blob back (`ls $DISK_TREE_ROOT/scans/*.parquet`) as `dt-cloud index-write`'s input.

**CLI** (`cli/`):
```bash
disk-tree bulk-list URI    # Shard URI's object listing (gcs:// | s3:// | r2:// with `-E <endpoint>`) into
                          # layer-1 parquet shards at `-o` (`-a` adaptive range-splitting, `-P` procs).
                          # `hf://<ns>[/<kind>[/<name>]]` (`find/bulk_hf.py`, `HF_TOKEN`) lists a Hugging Face
                          # namespace as bucket `<ns>`: `buckets/<b>/…`, `{models,datasets,spaces}/<repo>/…`
                          # (current tree; LFS deduped by oid) + `<repo>/@history/@unlisted` (what
                          # `usedStorage` bills beyond it), so non-`git` bytes sum to the billing page;
                          # `storage_class_id` public 1 / private 5 / unbilled git 6 (`import -s hf -p
                          # storage_class_id`). `-L` per-object history via `lfs-files` (write token), `-D`
                          # repo-level dates (50→1000 entries/page); `-w` = requests in flight (429s at 8)
disk-tree import -l GLOB…  # Aggregate listing(s) into a canonical layer-2 per bucket (`-b`; default every
                          # bucket in the listings): `-e pandas|duckdb|stream`, `-p` pivot sums, `-m`
                          # mtime_mean, `-i` also cut the path-store tiers, `-w/--to` write dir or URL

disk-tree recompress PATH…  # Rewrite v1 layer-2 listings as v2 in place, lossless (spec `listing-slim.md`
                          # phase 2): files, dirs (recursive `*.parquet`, sidecars skipped) or fsspec URLs
                          # (`gs://`, `r2://`, `s3://`). Streams by row group (never a whole file in memory):
                          # drops `uri` (scan root → metadata; refused unless `uri == <root>/<path>` on every
                          # row) and the `sum_*` pivots equal to `size`, re-encodes under
                          # `$DISK_TREE_PARQUET_CODEC` in ≤64K-row groups to a `.v2.tmp` sibling, verifies
                          # (row count + order-insensitive digest of `(path,size,mtime,kind)`), then swaps
                          # (atomic rename; copy + delete on a URL). v2 input is skipped; a failure leaves
                          # the original untouched, exit 1. `-n` dry-run, `-k` keeps `<stem>.v1.parquet`,
                          # `-j` JSON. Per-file old/new size + ratio, and totals
disk-tree listing-format PATH…  # The audit: each parquet's listing format (v1 | v2 | not-a-listing), codec,
                          # row groups, rows, size (+ root/implied for v2) from the footer only; `-j` JSON

disk-tree tiers L2        # Cut the path store's sorts (spec `path-store.md` §1.2/§4.1) from a layer-2 — a
                          # local path or an fsspec URL (copied once through `blobfs.open_read`): `path` =
                          # every row (objects + dirs) sorted `(depth, path, …labels)`; `bysize` = the same
                          # rows sorted `(⌊log2 size⌋ desc, path, …labels)`, size 0 last, the bucket computed
                          # in SQL (never stored); over label slices each row carries its path's total `tot` and
                          # the sort keys on `⌊log2 tot⌋` (spec `bysize-path-total.md`; the `-by-usr` copy stays
                          # per slice). 8K-row groups (`-r`), `tier`/`sort` (+ `bucket: log2`) in
                          # the parquet metadata, the source's listing format inherited. `-t path,bysize`
                          # (default both), `-s STEM` (may be a URL: cut locally, uploaded), `-g` writes the
                          # `.groups.json` footer sidecar beside each (`find/groups.py`; carries `b_min` now),
                          # `-v usr` extra sorted copies led by those columns, `-j` JSON; `-m` DuckDB memory limit (default 8GB — an
                          # external sort, spills to `-T`, default `.duckdb-tmp` beside the stem; unbounded it took
                          # 28 GB for 57M rows), `-p` threads. Prints rows, groups,
                          # bytes, KV per tier. `import -i` does the same at import time (bare `-i` = both;
                          # the `dirs`/`objects`/`coarse` tiers are retired). The cloud overlay's
                          # `dt-cloud index-write` (cw) and `dt-cloud path-index -P` (gcs, the r2 demo)
                          # cut the same two sorts from their bucket unions — objects as rows, L2 column
                          # names — under `path-index[-bysize][-by-user].parquet` (spec §4.2–4.4)
dt-cloud index-recut SRC OUT  # Re-cut a published generation's `bysize` from its `path` sort into a NEW
                          # generation dir (never over one): `-c` checks root + depth-1 views against per-path
                          # sums over `path` (`bysize_check`); then `index-sync -v bysize -g <gen>` points at it
dt-cloud interval-store append [-c] [-n] SCAN_ID  # Append one scan to the interval store as a run beside its
                          # base generation (spec `interval-store.md` §2.7; profile `-P`/$INTERVAL_STORE_PROFILE):
                          # prepare → ranges (Batch) → publish (cut, binary-counter merges, manifest) → R2 → prune,
                          # strictly in scan-id order (exit 3: not published / not next; `-c` catches up)
dt-cloud static-names build -g GEN -L LAYOUT -s SCANS -o STORE -x off|MIN,TAIL  # The static name index's base generation
                          # on ONE machine (`static_local.py`): the staged (Batch) path's stages in-process — ranges,
                          # coalesced versions, suffix shards, catalog, drill (`build_drill`) + `roots-measure/`, and
                          # anchored search (`names/` + `anchors/`: `^q`, `^q$`, heavy `q$`, path terms; `-A` skips it) — over a
                          # work dir (`-w`), then copied to `<STORE>/static-names/GEN/` (a dir or `r2://`/`s3://` URL;
                          # never over a generation). SCANS + `-L` templates (`{id}`, `{gen}`) find the `path` sorts.
                          # The drill is chunked as the staged path (`-c` rows, default 4M: suffix rows per member-loop
                          # chunk, roots per short piece and per measured / rolled-up q range; roots spill as parquet),
                          # so its working set is `-c`-bound, not the corpus's; byte-identical at any `-c`. Logs a
                          # `build GEN: drill/<step>` line per sub-step (seconds, disk / RSS peaks, row counts)
dt-cloud static-names add -g GEN -L LAYOUT -s SCANS -o STORE  # Append every scan after GEN's newest as a run (append, shards,
                          # catalog delta, drill run, + `names/`/`anchors/` when GEN's base has them — an anchorless GEN
                          # gets them only by a rebuild into a new GEN — `manifests/<id>.json`), then the binary counter's carries; a URL
                          # store's generation is mirrored into `-w`, new files go up data → `meta.json`s → manifests.
                          # Small deployments' path (the r2 demo's daily ingest); gcs/cw run the stages on Batch
dt-cloud interval-store build -g GEN -L LAYOUT -s SCANS -o STORE  # The interval store's base generation on ONE machine
                          # (`interval_local.py`, spec `interval-store.md` §2.8): ranges, path/read versions, fold,
                          # owner slices, every served sort — the staged stages in-process over `-w` — then copied to
                          # `<STORE>/interval-store/GEN/` (a dir or `gs://`/`r2://`/`s3://` URL; `scans.json` last;
                          # never over a generation). The staged per-range Batch stage is `interval-store build-ranges`
dt-cloud interval-store add [-g GEN] -L LAYOUT -s SCANS -o STORE  # Append every scan after GEN's newest as a run (per-range
                          # append over the open versions refolded from base + runs, run cut, `manifests/<id>.json`),
                          # then the due carries; a URL store mirrors into `-w`, uploads data → `meta.json`s → manifests.
                          # Reports peak work-disk/RSS per stage and scan. Every interval-store key goes through
                          # `blobstore.Store` (a profile's bare bucket name = `gs://`); `r2`/`r2-copy` are no-ops when the
                          # store is the serving R2 bucket. Every 2^L scans (`-C L`, default 5; `none`: never) it compacts
                          # base + runs into a new generation (`interval_compact`: a fresh build's bytes), then moves
                          # `interval-store/CURRENT.json` to it (the site follows it when `INTERVAL_STORE_GEN` is unset);
                          # `-g` defaults to that pointer. `interval-store current -o STORE [-s GEN]` prints / sets it
disk-tree tiers plan SIDECAR P THR  # The reader's span selection run offline over a tier's `.groups.json`
                          # (phase 0's instrument): for `path` it mirrors `readRects` exactly (depth rect
                          # `dP+1..`, path range `[P/, P0)`, `b_max ≥ thr·atten^(d−dP−1)` per group); for
                          # `bysize` it is `b_max ≥ thr_min ∧ p_max ≥ P/ ∧ p_min < P0`. Reports groups
                          # selected, rows they hold, bytes (compressed chunks), and — from the parquet —
                          # rows that actually pass, i.e. the decode waste. `P` = `.` for the root; `-a`
                          # attenuation, `-d` max depth, `-t` tier (default: from the name), `-C` skips the
                          # count, `-j` JSON. Measured on a 20K-child flat dir at 2048-row groups: `path`
                          # decodes 10 groups / 20,015 rows for 1,250 answers, `bysize` 1 group / 2,048
```

### Site (`site/`) and widget packages

`site/` is the hosted/serverless app: Vite + React + TypeScript, with Pages Functions over R2 / GCS +
D1 (r2.rbw.sh, the gcs/cw deployments). Its chart-lib-free DIY-SVG/canvas widgets come
from two workspace packages:
- **`@rdub/treemap`** (`packages/treemap/`) — the SOTA treemap core + layout/color primitives:
  `<Treemap>` (SVG + canvas renderers, shared-edge tiling, dust texturing, per-cell `ring`
  emphasis, hover/pin events), `<VoronoiTreemap>` (`/voronoi` subpath), `useHoverPin`, `squarify`,
  `DEFAULT_PALETTE`/`ageFade`/`parseQuery`, `styles.css`. Disk-agnostic; the intended external
  consumer (e.g. file-tree) pins this.
- **`@disk-tree/react`** (`packages/react/`) — `<TimeSeries>`/`<BytesOverTime>` built on the core
  (which it re-exports for back-compat).

### D1 schema (`site/schema.sql`)

One current-state file per branch (spec `done/d1-migrations.md`): new databases are created from it
(`wrangler d1 execute <db> --remote --file site/schema.sql`) and the tests build from it (`sqliteD1()`,
`conftest.d1_db`). A change is a recipe `site/migrations/<slug>.sql` plus `schema.sql` regenerated in the
same commit (`dt-cloud d1-schema dump site/schema.sql site/migrations/<slug>.sql -o site/schema.sql`;
`dt-cloud d1-schema check -p` verifies, and CI runs it); the recipe is applied to each of the branch's
databases (`--file site/migrations/<slug>.sql`; `cloud`'s only one is the r2 demo's `disk-tree-demo-db`),
then `git rm`'d. `dt-cloud d1-schema check -d <db> [-c <wrangler config>]` compares a live D1, read-only.

### Cloud site auth routes (`site/functions/`)

One gate (`_lib/auth.ts`, `@open-athena/auth` over D1): `identify` → `Identity` (`via: session | grant | public`), `requireViewer` / `requireStager` / `requireAdmin`.
- `/auth/google` (+ `/callback`, `/onetap`) — Google OIDC → email session; `/auth/email/*` — emailed code / magic link → email session
- `/api/auth/*` — the package's routes: `whoami`, `exchange` (`?key=` share link → grant session), `logout`, request-access, admin grant/request/log console
- `/api/token` — a session's personal agent token (a non-expiring Bearer grant, the base scope only; grants can't mint one)

## Development

During ongoing implementation, treat a concrete bug report about the code we own as an implied request to fix it: reproduce, implement, verify, and coordinate the deployment handoff without requiring a second prompt. Honor explicitly read-only requests. Code-fix authority does not imply unrelated infrastructure applies, permission changes, or another session's live cutover. Report separately what is fixed locally and what is actually deployed.

When another agent sends a useful, in-scope handoff, proactively review, implement or adopt, test, and coordinate it. A review queue tracks work; adding an item is not completion. Prefer coherent shared-code picks over merging deployment branches, preserve unrelated changes, and leave deployment-specific applies and live cutovers with their owners.

```bash
# Python setup — one uv workspace: the engine (root) + the cloud overlay
# (`cloud/`, package `dt-cloud`) share ONE `uv.lock` and one `.venv`.
uv sync                                                 # engine only
uv sync --all-packages --all-extras --all-groups        # engine + dt-cloud, every extra, test groups
disk-tree bulk-list -a s3://<bucket> -o work/listing/<bucket>
disk-tree import -e stream -s s3 -b <bucket> -l 'work/listing/<bucket>/shard-*.parquet'

# Site
pnpm install
cd site && pnpm dev
```

## Packaging / Distribution

`uv build` builds the engine's wheel (`src/disk_tree`; CLI only); there is no release workflow.
`dt-cloud` (`cloud/`) is a workspace member installed from the lock, not published.

## Data Flow

1. `disk-tree bulk-list` writes a bucket's sharded object listing
2. `disk-tree import` aggregates it by directory into a layer-2 parquet (+ a `Scan` row), or
   `dt-cloud path-index` aggregates the listings directly (gcs, the r2 demo's daily ingest)
3. `dt-cloud path-index` / `index-write` / `disk-tree tiers` cut the path store; `dt-cloud index-sync`
   publishes its footers to D1
4. `site/`'s Pages Functions read the path store (row-group range reads) and render treemaps / diffs

## Config

Default paths (override with `DISK_TREE_ROOT`):
- `~/.config/disk-tree/disk-tree.db` — SQLite `Scan` rows (`import`)
- `~/.config/disk-tree/scans/` — `import`'s layer-2 blobs
- `~/.config/disk-tree/buckets.yml` — per-bucket `endpoint_url` / `profile`

`import --to <dir|url>` (`config.set_write_target`) writes the blobs elsewhere — a local dir or an **fsspec URL** (`r2://bucket/prefix`, `s3://…`, `gs://…`); `DISK_TREE_SCAN_DIRS` (colon-separated, the first is the write target) sets it from the env. `r2://` rides s3fs with the bucket's endpoint from `DISK_TREE_R2_ENDPOINT_URL` or its `buckets.yml` entry. Every parquet read/write goes through `blobfs.py` (the local-vs-URL seam), and blobs are written in 64K-row groups so a `depth`/`path` pushdown over R2 fetches kilobytes.

**Cross-account credentials** — a `buckets.yml` entry (or `defaults`) may carry a `profile:` naming an AWS credential profile (`blobfs.bucket_profile`), so a source and a target in *different* accounts each authenticate with their own key. It threads to every S3/R2 seam: the `s3fs` blob IO (`_s3fs(endpoint, profile)`) and the `boto3` bulk lister (`S3BulkLister(profile=…)`, `bulk-list -f`). No profile → ambient credentials (env / default profile), the single-account default. Cross-account needs per-bucket endpoints too, so leave `DISK_TREE_R2_ENDPOINT_URL` unset (it globally overrides all per-bucket endpoints).

Stream-engine tuning knobs (env, all with measured defaults — see the constants block in `find/aggregate_stream.py`):
- `DISK_TREE_FLUSH_ROWS` — output row-group size (read-side: smaller = less fetched per directory browse, bigger footer)
- `DISK_TREE_PARALLEL_FINALIZE_MIN_ROWS` — below this the finalize stays serial even at `-j N`
- `DISK_TREE_SCAN_BATCH_ROWS` — pass-1 read-batch size (partition balance / seek granularity)

## Tests

```bash
pytest tests/                    # engine
cd cloud && pytest               # dt-cloud (same venv; sync with --all-packages first)
```

Tests build their listing fixtures inline (`tmp_path` parquets). CI and the job images install `--frozen` from the workspace lock (`deploy/sheet-mirror/Dockerfile` is the reference recipe: `uv sync --frozen --no-dev --no-editable --package dt-cloud --extra …` into `UV_PROJECT_ENVIRONMENT=/usr/local`); a plain `pip install .` resolves fresh and ships pins the tests never ran.

## Performance

- Depth column enables parquet predicate pushdown (only load needed rows)
- The site reads the path store by row-group range reads, planned from footers in D1 (`index-sync`)

## Deployments: parity by default

Every deployment (gcs, cw-s3, r2, hf, the r2 demo, …) ships the current full feature set. A new deployment, or a feature landing on `cloud`, reaches each of them without being asked; only a store that can't support a feature skips it, and its deployment spec says which and why. The baseline today:
- **Hosts:** prod and a dev stack: `dev.<host>` (a `branchAliases` CNAME onto the Pages branch `dev`), wrangler's `[env.preview]` with prod's bindings, preview-env secrets, every origin + callback (prod, dev, the `pages.dev` alias, localhost) on the Google OAuth client.
- **Auth:** app sign-in (Google OIDC + emailed codes), staff domain, admin console, personal agent tokens.
- **Serving:** the interval store as the Map's path store (`PATH_STORE = "intervals"`, following `interval-store/CURRENT.json`, once `interval-store verify` / a `ps=iv` parity check passes on the first scans); search from the prebuilt static name index only (`FILTER_STATIC`, `NAME_SUMMARY_STATIC`, `FILTER_STATIC_HEAVY`, `FILTER_INDEXED_ONLY = "1"`), no per-scan search sidecars. Per-scan `path`/`bysize` sorts are on their way out (`specs/lean-scans.md`).
- **Job:** `scan-run supervise` (phases, alerts, `/scans` records, cost labels), `index-gc`, cache warm-up (`warm-cache`) and the serving probe (`probe -c`) after each scan, the Slack digest thread.
- **Meta:** the "data behind this site" store (`STORES_JSON` `meta`) with its own static index.

A deployment's spec lists the baseline items it doesn't have yet as gaps, and those gaps are open work, not follow-ups to propose.

## hf

This branch is the hf.oa.dev deployment: the shared `cloud` base (everything above) plus the Hugging Face store, the org `open-athena`'s Storage Buckets and model, dataset and space repos as one bucket. This section covers what is hf's own. Apply state and the ordered checklist: `specs/hf-deployment.md`.

The repo is **public**: no emails, account ids, tokens or other secrets in tracked files or commit messages. The CF account and zone ids come from the environment (`$CLOUDFLARE_ACCOUNT_ID`, `$CF_ZONE_ID`), never from `Pulumi.hf.yaml`; the job gets the account id and the R2 endpoint from Secret Manager.

### Branches

- `hf` merges the local `cloud` branch (`git merge refs/heads/cloud`); never rebase onto it. Shared fixes are `[cloud]` commits, cherry-picked up by the root session; deployment commits are `[hf]`.
- D1: `oa-hf-usage-db` is on the shared `site/schema.sql` (`dt-cloud d1-schema check -d oa-hf-usage-db`); a change is a `site/migrations/<slug>.sql` recipe applied with `--file`.

### Layout (hf's own)

- `job/profiles/hf.yml`: the daily scan on the generic Nextflow pipeline (`specs/pipelines-everywhere.md`, since 2026-10-17): `dt-cloud scan head` runs `job/nf/scan.nf`, each step `dt-cloud scan step <name>` in its own Batch task (e2-standard-4; the listing e2-standard-2) — `list` (`disk-tree bulk-list hf://open-athena` + `import -e duckdb -p storage_class_id`) → `index` (`index-write`: the path store; `path-index` without `-P`: the snapshot JSONs) → `static-names` (one-machine `add|build`, into R2) + `publish-r2` (the served subset to R2, a barrier) → `index-sync` → `interval-store` → `index-gc` → `post` (`warm-cache`, `probe -c`, the digest: `-T cw -C job/digest.yml`, its root on R2, the Slack thread in #hf-usage). Run records under `job/hf-scan-runs.json`; every alert in #hf-admin. The D1 and the static-names generation are the site's (`site/wrangler.toml`; `infra/tests/test_hf_stacks.py` checks the profile agrees). `job/profiles/hf-scratch.yml`: a scratch run (one space, the Hub token only, under `scratch/`).
- `job/hf-batch-submit.sh`: the Batch spec of the head (e2-standard-2; `PIN=1 DRY=1` prints the cron body). `job/build.sh` + `job/Dockerfile` + `job/cloudbuild.yaml`: the `:hf` image (Cloud Build, a staged minimal context; Nextflow + its Google plugins, no site build; wrangler only for the digest's plot upload). `job/slack/`: the HF Usage Bot app (manifest, icon); `job/icons-hf/`: the digest's avatars, served by the icons Pages project `oa-hf-usage-icons`.
- Stores: the steps hand off through the GCS data bucket `oa-hf-usage-data` (infra/gcp; Nextflow's work dir `nf/`, `raw/<scan>/` the listing shards + the lister's `_SUCCESS.json`: billed-bytes reconciliation, request count; lifecycle: `raw/` 30 d, `nf/work|cache/` 14 d, `scratch/` 7 d). The site reads R2 (`oa-hf-usage-index`), under the same keys `publish-r2` copies: `listing/<scan>/index/<gen>/` (tiers), `listing/<scan>/open-athena.parquet` (layer-2), `snapshots/hf/<scan>/`; and R2 only: `static-names/<gen>/`, `interval-store/<gen>/`, `digest/hf/`. Scan ids are timestamps (`YYYY-MM-DDTHHMM`, as gcs/cw); the first, `2026-10-15`, is a date.
- `site/wrangler.toml` + `site/src/stores/hf.tsx`: the store (`STORE = "hf"`, app sign-in, `STAFF_DOMAIN = "openathena.ai"`). The billing allowances (no API for them) are declared values in `hf.tsx` (`HF_ALLOWANCES`): the About panel, the root view's meters (`allowances`, per storage class) and `job/digest.yml`'s quotas. `hubLink` maps a path to its Hub page (`externalLink`).
- `infra/gcp/` (stack `hf`): the job account `hf-usage-job`, the `hf-usage` image repository, the `hf-usage-*` secret containers, the data bucket `oa-hf-usage-data`, the `hf-usage-snapshot` cron (daily 08:00 UTC), called as the submit-only `hf-usage-scheduler` (custom role: `batch.jobs.create`); the job account submits the step tasks as itself (`batch.jobsEditor`). `infra/cf/` (stack `hf`, entrypoint `hf_stack.py` over the shared `stack/`): the `oa-hf-usage` Pages project, `hf.oa.dev`, the D1 `oa-hf-usage-db`, KV `oa-hf-usage-cache`, R2 `oa-hf-usage-index` with its read/write token (the job's), a read-only token (the site's `STORE_*`) and the job's D1 token. `infra/cf/hf-secrets` delivers the outputs to Pages and Secret Manager without printing them; `infra/cf/run` runs pulumi with `CF_IAC_TOKEN`.
- Storage classes (`storage_class_id`): public 1, private 5, unbilled git 6; the layer-2 and the path store carry `sum_storage_class_id_{1,5,6}`.

### Dev workflow

- Tests: `infra/tests/test_hf_stacks.py` (both stacks under Pulumi's mocks, the cron body against the secrets and the profile); `cd site && pnpm build && pnpm vitest run`.
- A scratch run of the DAG on Batch (one space, under `gs://oa-hf-usage-data/scratch/`, no record or alerts): `PROFILE=/app/job/profiles/hf-scratch.yml DT_RECORD=off DT_ALERTS=off job/hf-batch-submit.sh`. Never list the whole org from the laptop.
- `site/deploy` (from `site/`): deploys to hf.oa.dev and pushes to `o` (`-P` skips the push); `CLOUDFLARE_ACCOUNT_ID` from the environment.
