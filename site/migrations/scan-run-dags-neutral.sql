-- The run DAG, engine-neutral: `dag` is the graph the writer reduced (`dt_cloud.run_dag`: `{nodes: [{id, label}],
-- edges: [[from, to], …]}`), `engine` the format it came from; the raw file stays in the run's archive
-- (`<work>/runs/<scan>/`). Recreated, not altered: a row in the old shape is raw Mermaid the site no longer reads; a
-- run's DAG comes back with `dt-cloud scan-run dag RUN <work>/runs/<scan>/dag.mmd`.
DROP TABLE scan_run_dags;
CREATE TABLE scan_run_dags (
  run_id  TEXT PRIMARY KEY REFERENCES scan_runs(run_id),
  engine  TEXT NOT NULL,
  dag     TEXT NOT NULL
);
