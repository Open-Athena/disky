-- A phase's own Batch job and machine, when it ran apart from the run's job (a Nextflow step's task): the run page
-- links each step's job and logs. NULL for a one-VM run's phases.
ALTER TABLE scan_run_phases ADD COLUMN job_name TEXT;
ALTER TABLE scan_run_phases ADD COLUMN machine TEXT;
