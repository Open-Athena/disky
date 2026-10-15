# D1 migrations: what they're for here, and squashing them

Status: 2026-10-15. An explainer plus a proposal. Nothing has changed yet.

## Short answers

**Are migrations only there so mixed versions of the frontend, backend and database can work together?** No. That's a minor side benefit, and as you say it barely applies here: each deployment has one site and one database, deployed together.

What migrations actually do is **change a database that already holds data you want to keep**, without throwing that data away. Each deployment's D1 is long-lived, and much of what's in it can't be regenerated:

| Kind | Tables (cw lineage) | Regenerable? |
| --- | --- | --- |
| Sign-in and access | `allowed_emails`, `admin_emails`, `grants`, `agent_tokens`, `og_tokens`, `pending_auth`, `profiles`, `access_requests` | **No.** Who may sign in, issued tokens and share links. |
| Human actions | `plans`, `plan_items`, `stage_batches`, `stage_replies`, `deletion_runs` / `_bands` / `_progress`, `admin_edits` | **No.** Sweep plans, approvals and history. |
| Job and usage history | `scan_runs` / `_phases` / `_outputs`, `access_log`, `access_log_daily`, `session_log*` | **No**, or only by re-ingesting old logs. |
| Index pointers and caches | `index_schema`, `index_row_groups`, `footer_cache`, `owner_prefix_aggs` / `_hist` | Yes, from the files in R2/GCS (`index-sync`, rebuilds). |

When a change needs a new column or table, or a table rebuilt, a migration does it in place: `ALTER TABLE … ADD COLUMN`, or create, copy, drop, rename. The rows already there survive. The alternative, dropping the database and recreating it from the newest schema, would lose the first three rows of that table.

**Does the full history need to stay in the worktree forever?** No. A migration file matters only to a database that hasn't applied it yet. Once every live database is past file N, files 1..N are dead weight. Here the set of live databases is small and known: gcs, cw-s3, m3, the r2 demo, r2, hf and their dev stacks, which share the prod databases. So squashing is safe whenever all of them are at the head.

**Why did a new deployment (hf) apply all of them?** Because `wrangler d1 migrations apply` is how the schema gets created, and the only schema this repo records is "file 1, then 2, …, then 20". A new deployment doesn't need the history, only the result. Replaying it does no harm, but it's noisy: `0001_init` creates `marks`, `marks_new` and `mark_log`, and `0004_drop_marks` drops them again.

## How wrangler decides what to apply

Each database has a `d1_migrations` table holding one row per applied file name. `apply` runs every file in `migrations_dir`, in order, whose name has no row yet. Rows for files that no longer exist aren't a problem; those names are just never looked at again. This is what makes squashing straightforward: replace the files with one baseline, and record that baseline as already applied in every existing database.

## Where the noise actually comes from

- **Two lineages.** gcs has its own numbering (`site/migrations/gcs/`, 20 files numbered up to 0047) and everyone else uses `site/migrations/cw/` (20 files). The same change lands twice under two numbers, so `twins.json` and `scripts/port-migration` exist only to keep the pairs straight. gcs also has tables the cw lineage lacks (`actions`, `claims`, `keep_prefixes`, `mark_totals`, `owner_prefixes`, `owner_totals`, `session_monitor`, `sweep_approvals`, `user_emails`), and cw has one gcs lacks (`admin_emails`).
- **Created, then dropped.** `marks*`, `mark_log` and `index_schema_new` come and go across the history.
- **Ceremony per change.** Each new column means a file in each lineage, a twin entry, and an apply on every database.

Only the last item is the real cost of having a persistent database. The first two are history we're keeping without needing it.

## Proposal: squash to a baseline, one lineage

1. **Pin the target schema.** For each lineage, the baseline is what applying every file to an empty SQLite database produces. Check that it matches each live database's actual schema (`sqlite_master`, normalized), with `PRAGMA foreign_keys=ON` (memory `d1-enforces-foreign-keys`). Any mismatch is drift to fix first.
2. **Merge the lineages.** One `site/migrations/0001_baseline.sql` holding the union of tables. gcs-only tables stay in the baseline as empty tables on the other deployments (cheap), or move to a gcs-only extra file if we'd rather keep them out. Drop tables nothing reads anymore (check the code). Then `twins.json` and `port-migration` go away.
3. **Mark the baseline applied** on every live database (one write each, so each is a prod write needing your go): `INSERT INTO d1_migrations (name) VALUES ('0001_baseline.sql')`. Their old rows stay and are harmless.
4. **Delete the old files.** Tests that build databases from the migrations (`sqliteD1('cw')`) apply the baseline instead.
5. **Going forward:** a schema change is still one file (`0002_…`), since live databases hold data the change must keep. Squash again whenever the files pile up and every database is at the head, say every few months, the same way.

## What doesn't change

- **A schema change still needs a migration file.** The live databases hold sign-ins, tokens, sweep history and run records; a migration is how a change keeps them.
- **Deploy order still matters, briefly.** Apply the migration, then deploy the code that reads the new column. Additive changes (new table, new nullable column) keep the old code working in between. That's the one place "different versions running together" applies: a minute or two during a deploy.
- **Some data could move out of D1 entirely.** For the regenerable tables, a rebuild could replace a migration. Not worth doing on its own; mentioned only for completeness.
