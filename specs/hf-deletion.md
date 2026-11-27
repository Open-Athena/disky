# hf deletion: staged deletes on the Hub, dispatched by an admin

**Status:** proposed 2026-11-27 (Ryan: "a hf deletion executor is worth building … It should not try to execute automatically"). Nothing implemented.

## Goal

Free space in the `open-athena` org the same way other deployments do: people stage what they want gone on `/staged`, an admin reviews and dispatches a job, and the job deletes exactly what was reviewed. Nothing runs automatically. A quota alert or runway mark is a reason for people to stage things, never a trigger.

disky doesn't decide what to keep. Choosing checkpoints (every Nth step, the newest K) is the owners' call. They can stage by hand or script it with a personal agent token against the staging API, as on other deployments.

## Undo

The Hub has no restore. A deleted bucket file, a purged LFS object or a squashed history is gone. That's fine: undo is off or unsupported on every deployment now, and deletion doesn't depend on it.

No new abstraction is needed. Run records already carry an undo state that the shared UI reads (`softDelete.ts`: `runHasNoUndo`, `runNoUndoText`). An hf run records no retention and shows "no undo" like any run without soft delete. I'm not adding a `NopUndoExecutor`, because there is no `UndoExecutor` interface to implement: undo is a separate GCS-only route (`sweep_exec.undo_run`) gated by what the run recorded. The dispatch dialog states the run is irreversible.

## What a staged path means

hf's listing (`find/bulk_hf.py`) puts the org's storage under four kinds of path. Each maps to one Hub operation:

| staged path | operation | frees billed bytes? | precondition |
|---|---|---|---|
| `buckets/<b>/<path>` | `POST /api/buckets/<ns>/<b>/batch`, `deleteFile` lines (≤1000 per call) | yes, immediately | the file's `xetHash` now (`paths-info`) equals the reviewed one; checked just before the call (the batch has no per-file precondition, so this leaves a small race window, as R2's per-key path does) |
| `{models,datasets,spaces}/<repo>/<file>` (the current tree) | a commit of `deletedFile` ops with `parentCommit = H`, then purging each file's LFS oid that no kept ref still references (`POST …/lfs-files/batch`, `rewriteHistory: true`) | only through the purge: a deletion commit alone frees nothing | each staged file's oid at `H` equals the reviewed one; if the commit is refused because the head moved, re-read `H` and re-check every staged file (delete-preconditions.md §4) |
| `<repo>/@history/<ref>/<filename>[/<oid>]` (old LFS versions) | purge those oids (`lfs-files/batch`, `rewriteHistory: true`) | yes; the Hub says quota reflects it within ~36 h | each oid is a content hash, so it either exists or doesn't; it's kept if any file at the head of a kept ref references it |
| `<repo>/@history/<ref>/` whole, `<ref>` ≠ the default branch | the purge above, then `DELETE …/branch/<ref>` | yes (the purge) | as above; a ref with files at its head that aren't staged isn't deleted |
| `<repo>/@history/@unlisted`, a whole repo, the default branch | refused | | |

Mochi's ~58 checkpoint branches (62 TB under `models/Marin-Mochi-535B-A23B/@history`) are the fourth row: staging `@history/step-N/` purges that checkpoint's objects and removes the branch.

A purge can hit other refs, because LFS storage is deduplicated by content. So the reference check reads the tree at the head of every ref the run keeps: every ref of the repo except the ones being deleted. Any oid found there is kept and reported as `kept_referenced` with the ref that holds it. It is never deleted.

Deleting a whole repo (`DELETE /api/repos/delete`) is out of scope until someone asks for it.

## Phases

### 0. Each file's hash through the listing and the interval store

The executor checks a file against the hash it was reviewed at, so staging has to record that hash.
- `bulk_hf.py` writes the `version` column: the LFS sha256 or git blob oid for repo files, `xetHash` for bucket files. It already reads all three.
- hf's interval store adopts the string `object_version` (`interval-store migrate-object-version` into a new generation). Historical rows stay NULL. Verify the new generation, then move `CURRENT`, dev first.
- A row with no `version` can't be deleted. The executor refuses the manifest, as cw's does, and the stager re-stages after a fresh scan.

### 1. Staging on hf

The parity gap: hf has no `STAGING`, `/staged`, staged-plan Slack thread or `staged-card` step. Turn them on as cw has them: `STAGING = "1"`, `SLACK_STAGED_CHANNEL` = #hf-usage, and the `staged-card` step in `job/profiles/hf.yml`. Dev first, then prod.

The staging UI marks the refused paths from the table above as not stageable on this store.

### 2. The executor: `dt-cloud hf-sweep` (hf branch)

This code is hf's own (branches carry exactly their code), beside `bulk_hf.py`.
- **Input:** the plan built by the shared plan step from the interval store (`sweep_source.review_source`): reviewed paths, sizes and `version`s.
- **Order:** bucket deletes, then current-tree commits per repo, then purges per repo (reference check first), then branch deletes.
- **Dry run by default:** it runs every check and the reference check, and writes the decision log (`deleted` | `skipped_overwritten` | `skipped_gone` | `kept_referenced` | `delete_failed`) and the bytes it expects to free. It writes nothing to the Hub.
- **`-x`:** the real run.
- **Failures:** a failed call is `delete_failed` and the CLI exits nonzero. Nothing is retried with a fresh hash.
- **Records:** the run goes into `deletion_runs` through the shared `record_run_start` / `record_run`, with no retention (no undo). Progress uses the same progress feed as other runs.
- **Token:** a write-scoped Hub token. `hf-usage-hf-lfs-token` is write-scoped for `lfs-files`; whether it also covers commits, branch deletes and bucket batches is to be checked. Minting a new one is Ryan's.

### 3. Dispatch

`/staged`'s "Delete for real…" dispatches the executor as a Batch job (`SWEEP_IMAGE` = the `:hf` digest, `SiteDispatcher` `sweeps`), as on gcs/cw. This needs:
- `infra/gcp` + `infra/cf` applies, Ryan's go;
- `SWEEP_DISPATCH = "0"` on the dev stack, as today.

The dispatch dialog says what each kind of path will do (the table above) and that none of it can be undone.

### 4. First real run

1. Ryan's go.
2. A canary on objects we own: a scratch bucket file, and a throwaway repo with two branches sharing one LFS file (the reference check must keep it).
3. Then the first staged plan someone actually wants.

Quota reflects a purge within ~36 h, so the run record shows "expected to free"; the next scans' `usedStorage` confirms it.

## Open

- The status code the commit endpoint returns when `parentCommit` is stale (409 or 412: not documented). Find it on the canary repo.
- Whether deleting a branch alone frees anything (not documented). The design doesn't rely on it.
- A scripted-staging example (agent token → staging API) for checkpoint owners, in the docs.
