# Re-running a scan's failed step

Status: built (cw session, 2026-11-28; Ryan: "Phase 2 sg go"). Phase 1: `scan_dag.only`, `scan head --only` (unrecorded without `$DT_RECOVERS`). Phase 2: `scan-run rerun` (`scan_rerun.py`), and a head with `--only` and `$DT_RECOVERS` recording itself as a `phase-recovery` child of the scan's run, which /scans already reads as recovering the parent's failed steps (`scanRunsModel` `recoveryAttempts`), so no schema change. Dry-run checked against cw's D1 and Batch (2026-11-28T0602's digest). Ryan, on cw's two "partial · digest" scans: "something we should have a proper CLI for … could be a subset of resuming the failed scan (digest step only?)".

## Why

A soft step that fails (cw's `digest` on 11-28: Slack refused the OP's 4,022-char text) leaves its scan "partial" until the next scheduled scan converges it, hours later. Once the cause is fixed (a promote) nothing re-runs the step for that scan: the only way today is a hand-built Batch job copying the step's env and secret bindings, which nobody should write by hand.

## Design: the head, resuming the scan, over a subset of its DAG

`scan head` already resumes a scan (`$SNAP_ID` / `$GEN` / `$DT_JOB_START` set: Nextflow's `-resume <session>`, fray's `.done` markers), and each step is idempotent (`scan step`: done work is skipped). So a re-run is the head again, for that scan, told which steps to run:

1. **`scan head --only STEP` (repeatable).** The DAG cut to those nodes (`scan_dag`): their deps are taken as done, not run (they did run: the scan got that far), and nothing downstream is added. Both backends (the rendered `scan.nf`, fray). The head records into the scan's run (`$DT_RUN_ID`, the existing record), each re-run step's phase replacing the failed one, so `/scans` reads the run as it ends up (a `partial · digest` run whose digest now succeeds: recovered).
2. **`dt-cloud scan-run rerun SCAN -s STEP [-n]`.** Looks the scan's run up (`scan_runs`: its run id, generation, job start, profile), then submits the deployment's head job as its scheduled submitter does (the same spec, image, labels, secrets as references), with that scan's identity in the env, `DT_TRIGGER=rerun`, and `--only STEP`. `-n` prints the spec. It refuses while any job of that scan is live, and a step whose failure table (`scan steps`) says it doesn't re-run alone (a step a later scan converges is fine; a core step isn't: a failed core step means the scan didn't finish, and the whole head resumes it).
3. **Recorded.** As a `phase-recovery` child run of the scan's run (`start -k phase-recovery -p <run>`): its trigger `manual`, by `$DT_TRIGGER_BY`; the re-run steps record their phases into it as any step does, and /scans reads a succeeded child as recovering the parent (`recovered · <step>`).

## Not here

- Re-running a core step (a resume of the whole head is the existing path).
- Running a step under a different image than the deployment's live one (the re-run uses the live image: re-running after a promote is the point).
