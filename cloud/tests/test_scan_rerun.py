"""`scan-run rerun` (specs/scan-step-rerun.md phase 2): the scan's head job submitted again with `--only` its failed soft
steps and the run it recovers (`rerun_spec`), the run it targets (`rerun_target`), and the steps it refuses."""
from __future__ import annotations

import datetime as dt

import pytest
from click.testing import CliRunner

from dt_cloud import scan_rerun as R
from dt_cloud import scan_runs
from dt_cloud.d1_schema import table_ddl
from dt_cloud.scan_runs import SqliteSink

DDL = table_ddl(scan_runs.TABLES)
SCAN = "2026-11-28T0603"
UID = "cw-usage-snapshot-5c2c02e9-1b45-41b600"

#: `gcloud batch jobs describe` of a cw head job, trimmed.
HEAD = {
    "name": "projects/p/locations/us-central1/jobs/cw-usage-snapshot-20261128-06", "uid": UID,
    "status": {"state": "SUCCEEDED"}, "createTime": "2026-11-28T06:00:01Z", "updateTime": "2026-11-28T08:40:00Z",
    "taskGroups": [{
        "name": "projects/p/locations/us-central1/jobs/cw-usage-snapshot-20261128-06/taskGroups/group0", "parallelism": "1", "taskCount": "1",
        "taskSpec": {
            "runnables": [{"container": {"imageUri": "reg/scan:latest", "entrypoint": "dt-cloud", "commands": ["scan", "head", "/app/job/profiles/cw.yml"]}}],
            "environment": {"variables": {"DT_TRIGGER": "schedule", "DATA_BUCKET": "d"},
                            "secretVariables": {"SLACK_BOT_TOKEN": "projects/p/secrets/s/versions/latest"}},
            "computeResource": {"cpuMilli": "2000", "memoryMib": "7000"}, "maxRunDuration": "21600s"}}],
    "allocationPolicy": {"instances": [{"policy": {"machineType": "e2-standard-2"}}], "serviceAccount": {"email": "sa@p"},
                         "location": {"allowedLocations": ["regions/us-central1"]}, "labels": {"app": "disky", "deployment": "cw", "component": "scan"}},
    "labels": {"app": "disky", "component": "scan", "deployment": "cw"},
    "logsPolicy": {"destination": "CLOUD_LOGGING"},
}


def test_rerun_spec_is_the_head_with_only_and_the_run_it_recovers():
    """The head's task, policy and labels as they are, nothing Batch filled in; the command given `--only digest`; the
    scan's identity, `DT_RECOVERS` and a manual trigger by whoever starts it in the env; secrets the job's references."""
    spec = R.rerun_spec(HEAD, scan=SCAN, gen="20261128T060312Z", parent=UID, steps=["digest"], by="ryan")
    assert spec == {
        "taskGroups": [{"taskCount": 1, "taskSpec": {
            "runnables": [{"container": {"imageUri": "reg/scan:latest", "entrypoint": "dt-cloud",
                                         "commands": ["scan", "head", "--only", "digest", "/app/job/profiles/cw.yml"]}}],
            "environment": {"variables": {"DT_TRIGGER": "manual", "DATA_BUCKET": "d", "SNAP_ID": SCAN, "SCAN_ID": SCAN,
                                          "GEN": "20261128T060312Z", "DT_RECOVERS": UID, "DT_TRIGGER_BY": "ryan"},
                            "secretVariables": {"SLACK_BOT_TOKEN": "projects/p/secrets/s/versions/latest"}},
            "computeResource": {"cpuMilli": "2000", "memoryMib": "7000"}, "maxRunDuration": "21600s", "maxRetryCount": 0}}],
        "allocationPolicy": HEAD["allocationPolicy"], "labels": HEAD["labels"], "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }
    assert HEAD["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"] == ["scan", "head", "/app/job/profiles/cw.yml"]


def test_rerun_spec_refuses_a_job_that_isnt_a_head():
    job = {**HEAD, "taskGroups": [{"taskSpec": {"runnables": [{"container": {"entrypoint": "bash", "commands": ["job/cw-run.sh"]}}]}}]}
    with pytest.raises(ValueError) as e:
        R.rerun_spec(job, scan=SCAN, gen="G", parent=UID, steps=["digest"])
    assert str(e.value) == "the scan's head job doesn't run `dt-cloud scan head`: bash job/cw-run.sh"


def test_rerun_job_id():
    now = dt.datetime(2026, 11, 28, 15, 4, 5, tzinfo=dt.timezone.utc)
    assert (R.rerun_job_id("cw-usage-snapshot-20261128-06", now), len(R.rerun_job_id("x" * 70, now))) == (
        "cw-usage-snapshot-20261128-06-rerun-150405", 63)


def _sink(status: str = "succeeded", phases=(("index", "done"), ("digest", "failed")), child: str | None = None) -> SqliteSink:
    s = SqliteSink(":memory:", ddl=DDL)
    s.execute([
        "INSERT INTO scan_runs (run_id, store, scan, kind, status, gen, job_name, region, started_ts, source, updated_ts) "
        f"VALUES ('{UID}', 'primary', '{SCAN}', 'scan', '{status}', 'G', 'cw-usage-snapshot-20261128-06', 'us-central1', 100, 'job', 100)",
        *[f"INSERT INTO scan_run_phases (run_id, phase, seq, status) VALUES ('{UID}', '{p}', {i}, '{st}')" for i, (p, st) in enumerate(phases)],
        *([f"INSERT INTO scan_runs (run_id, store, scan, kind, parent, status, source, updated_ts) "
           f"VALUES ('rerun-1', 'primary', '{SCAN}', 'phase-recovery', '{UID}', '{child}', 'job', 200)"] if child else []),
    ])
    return s


def test_rerun_target_is_the_scans_run():
    assert R.rerun_target(_sink(child="succeeded"), "primary", SCAN, ["digest"]) == {
        "run_id": UID, "status": "succeeded", "gen": "G", "job_name": "cw-usage-snapshot-20261128-06", "region": "us-central1"}


@pytest.mark.parametrize("sink, scan, steps, why", [
    (dict(status="running"), SCAN, ["digest"], f"{UID} is still running"),
    (dict(child="running"), SCAN, ["digest"], f"{UID} has a re-run running (rerun-1)"),
    (dict(), SCAN, ["index"], f"{UID} has no failed phase index (failed: digest)"),
    (dict(), "2026-11-29T0003", ["digest"], "no run of scan 2026-11-29T0003 in store primary"),
])
def test_rerun_target_refuses(sink, scan, steps, why):
    with pytest.raises(SystemExit) as e:
        R.rerun_target(_sink(**sink), "primary", scan, steps)
    assert str(e.value) == f"scan-run rerun: {why}"


@pytest.mark.parametrize("steps, out", [
    (["index"], "scan-run rerun: index is a core step: resume the scan's whole head instead"),
    (["digest", "nope"], "scan-run rerun: no step nope"),
])
def test_rerun_refuses_core_and_unknown_steps(steps, out):
    """Before any lookup: a core step re-runs only with its whole scan (`scan head` resuming it), and a step must exist."""
    res = CliRunner().invoke(R.rerun_cmd, [a for s in steps for a in ("-s", s)] + [SCAN])
    assert (res.exit_code, res.output) == (1, out + "\n")
