# `/dups`: duplicate and likely-duplicate data across buckets

Ryan (2026-10-18): "a page just for GCS (for now), /dups, can be a report about dup and likely dup paths, making it easy to review and stage."

Marin's data lives in six regional buckets, and some datasets/checkpoints were copied between regions (training reads locally; cross-region reads cost egress). Some copies are deliberate, some are leftovers. `/dups` lists the candidates so an admin can review each group and stage the copies that aren't needed, through the existing staging flow (`/staged` → dry run → real delete).

## 1. Detection: `dt-cloud dups` (a scan step)

Input: the scan's per-scan path index (`<layer2>index/<gen>/path-index-bysize.parquet`, else `path-index.parquet`: dirs + objects with `path`, `size`, `n_files`, `n_desc`, `mtime`, `created`, `last_read`, class sums) — and, once listings carry it, the objects' `crc32c` (§4).

A **group** is a set of ≥2 paths judged to hold the same data (`moved` copies may share a bucket: one bucket can hold a dataset under several names). Tiers, highest first:

| tier | rule | confidence |
|---|---|---|
| `tree` | same relative path (the path minus its bucket) and identical `size` and `n_files` | high |
| `moved` | different relative paths, identical `size`, `n_files` and basename | needs a check |

A group is `exact` (a column beside its tier, so a `moved` group keeps its names and its id) when every object under its copies matches the keep copy's by relative name, size and `crc32c`: proven (§4).

Dirs and objects are both grouped when their total size (the sum of the index's owner slices) is ≥ a floor (`-f`, default 1 GiB); the `bysize` sort's `tot` column prunes the read to those paths.

Groups are **maximal**: a group is dropped when its copies' parents are exactly another group's copies (the report shows `marin-*/tokenized/foo/`, not each of its shards). A child group with a different copy set survives, and its copies under another group's copy are flagged `nested`: the `net_*` totals count only un-nested copies, so a byte counts once. Groups of ≥7 copies (`-s`) are flagged `suspect` (mostly same-size collisions, e.g. shards named alike) and totaled apart. Tier totals overlap (a `moved` group can span two `tree` groups): they're never summed.

Output, beside the scan's index: `<layer2>index/<gen>/dups.parquet`, one row per (group, copy):
`group, tier, rel, path, bucket, kind, size, n_files, mtime, created` (epoch s), `last_read` (epoch days), the class pivots (`sum_storage_class_id_*`), `copies, reclaimable, n_buckets, nested, suspect, keep, exact, check, check_n, check_eq`, sorted by the group's reclaimable bytes (size × (copies − 1)) descending, in 8K-row groups. `keep` is the suggested copy (the latest read, ties → the oldest). Plus `dups.json`: totals by tier (gross), net by tier, suspect, by bucket pair (net; a diagonal pair = ≥2 copies in one bucket), the copies histogram and the checks' verdicts. A scan step `dups` (post, soft, opt-in via the profile's `steps`, options `floor`, `check`, `check_objects`, `suspect_copies`, `mem`, `threads`, `listing`) runs it in the post task. `dups.json` (v2) adds `verified_copies` and `exact` (net totals of the exact groups by tier).

## 2. API: `GET /api/dups?date=<scan>`

Reads the scan's `dups.json` and `dups.parquet` from the index store (range reads, row group by row group, stopping at `min` or the `n`+1-th match) and applies the open plans' items per copy: `staged` (the plan covering it). Query params: `tier`, `min` (reclaimable bytes), `buckets` (one bucket or a pair), `q` (path substring), `suspect=1` / `nested=1` (included only on request), `n` (≤ 500). No `date`: the newest scan with a report. Viewer scope to read; staging stays admin, through `POST /api/plans/stage`. (Not done: `owner` per copy from the ledger, `deleted` from the run log.)

## 3. Page: `/dups`

- Header: reclaimable totals by tier, and by bucket pair (a small matrix: which regions duplicate which).
- One row per group: the relative path, tier, size, copy count, reclaimable bytes; expands to its copies: bucket (region), size, objects, created, last read, idle, owner, class mix, staged/deleted state.
- A suggested keep per group (the most recently read copy; ties → the oldest), the others pre-checked; the admin adjusts and clicks "stage N copies" (`useStage`), then finishes on `/staged` as today. Nothing deletes from `/dups` itself. A `moved` copy is stageable only once its spot check came back `equal`; a copy whose check `differs` never is.
- Filters: tier, min size, bucket pair, path text; sort by reclaimable, size, idle.
- Linked from the menu (gcs only for now: a deployment var `DUPS=1` gates the route and the step).

## 4. Exactness: `crc32c` in the listings ([cloud])

GCS returns `crc32c` for every object in the list response. `disk-tree bulk-list -c` (gcs:// only, opt-in: a listing without it is byte-for-byte what it was) keeps it as a nullable `crc32c` column (int64: the unsigned 32-bit checksum) after `generation`, in every shard of the run. S3/R2 list no checksum (`-c` refuses them); an MD5/ETag column is not done.

`dt-cloud dups -l <shards>` (the step's `listing: true`: each source's shards under `layout.listing` in the data store; an importing profile has none there and gets a warning) reads the scan's raw listing once, rather than carrying the checksum through `path-index` (as `-G` carries `generation`): the listing is already there in the post task (retire keeps the newest 2 scans'), and a per-dir digest in the index would touch every aggregation engine. Each object is matched to the copies it lies under (a dir copy by its first `d` path segments, one hash-join key per dir-copy depth `d`; an object copy by its name, after pruning to the copies' buckets and top-level dirs), and each copy gets its object count, how many carry a checksum, and an order-free digest (the sum of each object's hash of its name relative to the copy, its size and its CRC32C). A non-keep copy is `equal` to the keep copy when both are fully checksummed with the same count and digest, else `differs` (null without the checksums): `check` = that, `check_n` = the keep copy's objects, `check_eq` = all of them when equal. A differing `moved` copy is dropped (and its group when < 2 copies remain, or when it's left with a `tree` group's copies; `copies`, `reclaimable`, `n_buckets`, `suspect` follow; the child groups maximality dropped under it aren't restored); a differing `tree` copy stays, `differs`. A group whose every copy is `equal` is `exact`.

Groups without any verdict are what the spot check covers (`-g N`, by reclaimable bytes): each copy is listed on GCS (name, size, crc32c; ≤ `-n` objects) and compared to the keep copy: `check` = `equal` | `partial` (capped listings) | `differs`, with `check_n` / `check_eq` counts. The research pass found `tree` groups whose names and sizes all match but whose crc32c all differ, so `tree` is high confidence, not proof.

Cost (estimated, not yet measured on a real listing): a CRC32C is 4 random bytes (incompressible): ~4–5 B per object in the shards, ~2 GB on gcs's ~425M objects a scan (the raw listings are ~10.7 GB), kept for the newest 2 scans by retire's `layer1`. The `dups -l` pass reads the scan's `bucket, name, size_bytes, crc32c` once, on the post machine. To turn it on for gcs: `options.list.bulk_args: [-c]`, `options.dups.listing: true`, an image rebuild.

## Status

- 2026-10-18: a research pass on the 10-18 scan (Batch) measured the candidates and their tiers; its method and SQL seeded §1 (its per-slice size floor undercounted multi-owner paths: fixed by summing slices first).
- 2026-10-19: `dt-cloud dups` + the `dups` step, `/api/dups`, `/dups` (gated by `DUPS=1`) built; gcs enables the step and the var.
- 2026-10-19: §4 built (`bulk-list -c`, `dups -l`, `exact`; the page tags exact groups), not yet on in gcs's profile.
- Not started: `owner` / `deleted` per copy (§2); an MD5/ETag column for S3/R2.
