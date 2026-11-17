-- The agent that started a manual run for `trigger_by` (`$DT_TRIGGER_VIA`, `dt_cloud.scan_runs.TRIGGER_VIA_ENV`)
ALTER TABLE scan_runs ADD COLUMN trigger_via TEXT;
