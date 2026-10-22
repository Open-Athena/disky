# `users/<seg>/` attribution gap, and a Finelog writer signal

From the marin session, 2026-10-22, against scan `2026-10-22T0702` (read via `/api/subtree`, `/api/owners`, `/data/rules.json` in a signed-in browser).

## 1. Bug: `users/<seg>/` prefixes publish no owner

**Fixed 2026-10-22** (`3280a0ad`, cloud `da5fe26e`): the gcs job fed a frozen `attr/attribution-2026-07-20.parquet` (6 `user-prefix` rows) and never re-derived them; `load_prefix_map` now derives `user-prefix` rows from the listing being attributed. Takes effect with the 2026-10-23 daily (image rebuilt); 2026-10-22T0702 stays as published. Still open: the parquet's `artifact-record` rows are stale since July too.

`user_prefix_rows` should own every `gs://<bucket>/users/<seg>/`, and `IdentityMap.resolve` maps an unaliased segment to itself. But in the published path index these are unowned (`us` empty), although `rules.json` aliases most of them:

| Prefix | TB | Expected owner |
| --- | ---: | --- |
| `marin-us-east5/users/tonyhlee/` | 14.33 | tony-lee? (`tony` alias; `tonyhlee` not aliased → should still own as `tonyhlee`) |
| `marin-us-central1/users/tonyhlee/` | 8.54 | same |
| `marin-us-central2/users/tonyhlee/` | 5.29 | same |
| `marin-us-central2/users/held/` | 3.96 | will-held (alias `held`) |
| `marin-us-central2/users/benjaminfeuer/` | 1.32 | unaliased → `benjaminfeuer` |
| `marin-us-east5/users/benfeuer/` | 0.88 | unaliased → `benfeuer` (same person?) |
| `marin-us-central2/users/dlwh/` | 0.85 | david-hall (alias `dlwh`) |

Total ≈35 TB across buckets. Prefixes that *do* resolve: `users/root/`, `users/power/` (→ russell-power). Those segments were all last written by mid-September; the failing ones have writes in late Sep–Oct. Hypothesis to check first: the `user-prefix` rows come from a pre-filtered listing or an attribution artifact that predates these segments, so new `users/<seg>/` dirs never get a row.

Acceptance: every `users/<seg>/` in the latest scan has a `user-prefix` owner in the path index, and a regression test covers a segment that first appears after the previous attribution run.

## 2. New signal: Finelog writer (the unbuilt `iris-path`)

Every Iris task log line has `key = /<job-user>/<job>/…:<task>`, and checkpoint/output lines name the `gs://` path being written. A 6h sample on 2026-10-22 mapped writers to prefixes like:

| Job user | Writes under | Unowned TB today |
| --- | --- | ---: |
| `kaiyuew@stanford.edu` | `marin-us-central2/kaiyuew/belay-*` | 100.9 |
| `quevedo` | `marin-eu-west4/julian/` | (julian owns 23.5) |
| `bizon`, `zack` | `marin-us-east5/protein-structure/MarinFold/` | 5.4 |
| `michaelryan` | `checkpoints/isoflop-curation/` | already assigned |

Proposed shape: a daily in-region job (not a laptop; a 6h `created_at` slice of the log catalog was ~20 GB because compacted segments carry fresh creation times, so select on the segments' key/time ranges instead) that runs
`SELECT split_part(key,'/',2) AS job_user, regexp_extract(data, 'gs://(marin-[a-z0-9-]+/[^ ]+)', 1) AS path …` over lines matching save/write/checkpoint/output-path, rolls paths up to a run-dir depth, and emits `AttributionRow(source="finelog-writer", evidence="<job-id>@<epoch>")`. Precedence: below `user-prefix` and `artifact-record`, above `wandb-*`. A prefix written by several job users gets no owner from this signal (surface it in `gaps` instead).

Log retention starts 2026-09-03 (object catalog), so this only covers dirs written since then; older dirs stay on the W&B signals.

## 3. Not in scope here

The 1.72 PB unowned dataset pool (`raw/`, `tokenized/`, `normalized/`, `datakit/`, `documents/`) has no single writer. Whether it gets an owner is a policy call, not a signal.

## 4. Candidate: sibling name-family signal (review-only at first)

Within a run parent (`checkpoints/isoflop/`, `grug/`, `checkpoints/`), give an unowned run dir the owner of its owned siblings that share its name family, when exactly one owner shares the longest family prefix. Prototype over the ten largest run parents (scan `2026-10-22T0702`): 130.4 TB sits in unowned run dirs over 100 GB; a naive longest-common-prefix rule (≥12 chars and ≥60% of the name, unique owner) matches 46.1 TB.

The naive rule is not safe to publish. Sweep templates collide: `isoflop-<flops>-d<w>-L<l>-B<b>-` is shared by rohith-kuditipudi (96% of owned `marin-us-central1/checkpoints/isoflop/`) and will-held (94% of owned `marin-us-central2/checkpoints/isoflop/`), and the distinguishing part is the data-mix suffix (`comma-mix`, `dclm-default`). Before it becomes a signal: match on the name with numeric hyperparameter tokens masked, so the suffix counts; emit as `source="name-family"` below every evidence-backed signal; and ship it first as a `gaps` review column, not an owner.
