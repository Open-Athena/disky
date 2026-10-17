-- The run page's Nextflow view, for hf's switch to the generic pipeline (`cloud`'s `scan-run-phase-jobs`,
-- `scan-run-dags` and `scan-run-dags-neutral`, in one): each phase's own Batch job, machine and pipeline node (NULL for
-- a one-VM run's phases), and each run's DAG, engine-neutral (`dt_cloud.run_dag`).
ALTER TABLE scan_run_phases ADD COLUMN job_name TEXT;
ALTER TABLE scan_run_phases ADD COLUMN machine TEXT;
ALTER TABLE scan_run_phases ADD COLUMN task TEXT;
CREATE TABLE scan_run_dags (
  run_id  TEXT PRIMARY KEY REFERENCES scan_runs(run_id),
  engine  TEXT NOT NULL,
  dag     TEXT NOT NULL
);
