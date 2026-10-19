# `/dups`: duplicate and likely-duplicate data across buckets

Ryan (2026-10-18): "a page just for GCS (for now), /dups, can be a report about dup and likely dup paths, making it easy to review and stage."

Marin's data lives in six regional buckets, and some datasets/checkpoints were copied between regions (training reads locally; cross-region reads cost egress). Some copies are deliberate, some are leftovers. `/dups` lists the candidates so an admin can review each group and stage the copies that aren't needed, through the existing staging flow (`/staged` → dry run → real delete).

## 1. Detection: `dt-cloud dups` (a scan step)

Input: the scan's per-scan path index (`<layer2>index/<gen>/path-index.parquet`: dirs + objects with `path`, `size`, `n_files`, `n_desc`, `mtime`, `created`, `last_read`, class sums) — and, once listings carry it, the objects' `crc32c` (§4).

A **group** is a set of ≥2 paths in different buckets judged to hold the same data. Tiers, highest first:

| tier | rule | confidence |
|---|---|---|
| `exact` | every object under the paths matches by relative name, size and `crc32c` | proven |
| `tree` | same relative path (the path minus its bucket) and identical `size` and `n_files` | high |
| `moved` | different relative paths, identical `size`, `n_files` and basename | needs a check |

Groups are **maximal**: a dir is dropped when its parent is in a group with the same bucket set (the report shows `marin-*/tokenized/foo/`, not each of its shards). Leaf objects are grouped only when they aren't under a grouped dir and are ≥ a size floor (`-m`, default 1 GiB).

Output, beside the scan's index: `<layer2>index/<gen>/dups.parquet`, one row per (group, copy):
`group, tier, rel, path, bucket, size, n_files, mtime, created, last_read, class bytes`, sorted by the group's reclaimable bytes (size × (copies − 1)) descending. Plus `dups.json`, a summary (totals by tier and bucket pair) for the page header. A scan step `dups` (post, soft, opt-in per profile) runs it after `index`; on Batch, like every step.

## 2. API: `GET /api/dups?date=<scan>`

Reads the scan's `dups.parquet` (row-group range reads, like the path store) and applies the live ownership ledger and `/staged` state per copy: `owner`, `staged` (already in an open plan), `deleted` (logged). Query params: `tier`, `min` (bytes), `buckets` (a pair filter), `q` (path substring). Viewer scope to read; staging stays admin, through `POST /api/plans/stage`.

## 3. Page: `/dups`

- Header: reclaimable totals by tier, and by bucket pair (a small matrix: which regions duplicate which).
- One row per group: the relative path, tier, size, copy count, reclaimable bytes; expands to its copies: bucket (region), size, objects, created, last read, idle, owner, class mix, staged/deleted state.
- A suggested keep per group (the most recently read copy; ties → the oldest), the others pre-checked; the admin adjusts and clicks "stage N copies" (`useStage`), then finishes on `/staged` as today. Nothing deletes from `/dups` itself.
- Filters: tier, min size, bucket pair, path text; sort by reclaimable, size, idle.
- Linked from the menu (gcs only for now: a deployment var `DUPS=1` gates the route and the step).

## 4. Exactness: `crc32c` in the listings ([cloud])

GCS returns `crc32c` (every object) and `md5Hash` (non-composite) in the list response. `disk-tree bulk-list` keeps `crc32c` as a nullable column (S3/R2: NULL, or the ETag where it's a plain MD5, as a separate column). Then `tree` groups are upgraded to `exact` by a join, and `moved` ones confirmed or dropped. Until then, the step spot-checks a sample of object pairs per top group through object metadata reads (`-k N`) and records `checked: n/m equal`.

## Status

- 2026-10-18: a research pass on the 10-18 scan (Batch) is measuring the candidates and their tiers; its method and SQL seed §1.
- Not started: the step, API, page, `crc32c` column.
