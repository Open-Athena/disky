-- The run page's DAG view: each phase's pipeline node (a Nextflow task's `<process> (<tag>)`), and each run's DAG as
-- its head wrote it (`-with-dag` Mermaid, `scan-run finish --dag`). NULL / absent for a one-VM run.
ALTER TABLE scan_run_phases ADD COLUMN task TEXT;
CREATE TABLE scan_run_dags (
  run_id  TEXT PRIMARY KEY REFERENCES scan_runs(run_id),
  mermaid TEXT NOT NULL
);
