"""`dt-cloud scan head`'s pure parts: identity, session, history line, params + executor config (golden files from
the cw-s3-shaped fixture profile), the trace's first failure, and a dry run's files."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from dt_cloud import scan_head as H
from dt_cloud.cli import main
from dt_cloud.scan_profile import load

FIX = Path(__file__).parent / "fixtures" / "scan_profiles"
NOW = dt.datetime(2026, 10, 16, 12, 0, 1, tzinfo=dt.timezone.utc)


def test_identity_stamped_once():
    assert H.identity({}, NOW) == ("2026-10-16T1200", "20261016T120001Z", "1792152001")
    resumed = {"SNAP_ID": "2026-10-15T0600", "GEN": "20261015T060002Z", "DT_JOB_START": "17"}
    assert H.identity(resumed, NOW) == ("2026-10-15T0600", "20261015T060002Z", "17")


def test_session_id():
    """uuid5(NAMESPACE_URL, `disky/<deployment>/scan/<scan>`): the same scan resumes the same session anywhere, and
    cw-s3's matches its prototype head's (`cw-head.sh`), so a scan started there resumes here."""
    assert H.session_id("cw-s3", "2026-10-16T1200") == "06a5f601-f0b7-58a3-9817-fc0a8fee5e63"
    assert H.session_id("other", "2026-10-16T1200") == "8b68f678-566c-5e77-9089-7305bed64c57"


def test_session_id_per_store():
    """A store other than the primary namespaces its sessions (`disky/<deployment>/<store>/scan/<scan>`): the same scan id
    of the deployment's meta store is another run. The primary's is unchanged, so its sessions still resume."""
    import uuid

    assert H.session_id("cw-s3", "2026-10-16T1200", "primary") == "06a5f601-f0b7-58a3-9817-fc0a8fee5e63"
    assert H.session_id("cw-s3", "2026-10-16T1200", "meta") == str(uuid.uuid5(uuid.NAMESPACE_URL, "disky/cw-s3/meta/scan/2026-10-16T1200"))
    assert H.session_id("cw-s3", "2026-10-16T1200", "meta") == "43a92eb7-2c67-54bb-9a9a-4c56a2b389b0"


def test_start_args_carry_the_store():
    assert (H.start_args(load(FIX / "cw-s3.yml"), "G", "2026-10-16T1200"), H.start_args(load(FIX / "meta-store.yml"), "G", "2026-10-16T1200")) == (
        ["-B", "-g", "G", "2026-10-16T1200"],
        ["-B", "-g", "G", "-K", "meta", "2026-10-16T1200"],
    )


def test_params_golden_per_call_secrets():
    """The meta-store fixture: a source's own bindings (its list call's whole list) and a step's (`step_secrets`), each
    the profile's overlaid; a step with none of its own reads `secrets`."""
    p = load(FIX / "meta-store.yml")
    got = H.params(p, "<profile>", "2026-10-16T1200", "20261016T120001Z", "1792152001", "run-1",
                   H.session_id("example", "2026-10-16T1200", "meta"), {})
    assert got == json.loads((FIX / "meta-store.params.json").read_text())


def test_history_line():
    assert H.history_line(NOW, "2026-10-16T1200", "S") == "2026-10-16 12:00:01\t-\tscan_2026_10_16T1200\tOK\t-\tS\tnextflow run scan.nf\n"


def test_executor_config_golden():
    assert H.executor_config(load(FIX / "cw-s3.yml")) == (FIX / "cw-s3.executor.config").read_text()


def test_params_golden():
    p = load(FIX / "cw-s3.yml")
    got = H.params(p, "<profile>", "2026-10-16T1200", "20261016T120001Z", "1792152001", "run-1",
                   H.session_id("cw-s3", "2026-10-16T1200"), {"DT_RECORD": "off", "DISKY_LABELS": "stack=prod,team=x", "OTHER": "1"})
    assert got == json.loads((FIX / "cw-s3.params.json").read_text())


def test_params_reproc_fresh_and_extras():
    from dt_cloud.scan_profile import parse

    p = parse({"deployment": "fix", "executor": {"kind": "local"}, "work": "/w", "data": "/d",
               "layout": {"layer2": "l2/{scan}/", "snapshots": ""}, "sources": ["gs://a"],
               "steps": ["access-ingest", "healthcheck"], "skip": ["digest", "static-names"],
               "options": {"list": {"import": True}}})
    got = H.params(p, "{}", "2026-10-16", "G", "1", "", "S", {}, reproc=True, fresh=True)
    assert {k: got[k] for k in ("steps", "pre", "pre_label", "post", "over_time", "over_time_label", "skip_list", "cache", "env",
                                "work_bucket", "secrets_project")} == {
        "steps": ["bucket-check", "access-ingest", "list", "index", "publish-r2", "interval-store", "index-rows", "index-sync",
                  "over-time", "index-gc", "healthcheck", "warm-cache", "probe"],
        "pre": ["access-ingest"],
        "pre_label": "small",
        "post": ["index-gc", "healthcheck", "warm-cache", "probe"],
        "over_time": True,
        "over_time_label": "post",
        "skip_list": True,
        "cache": False,
        "env": {"REPROC": "1"},
        "work_bucket": "",
        "secrets_project": "",
    }


def test_local_executor_config():
    from dt_cloud.scan_profile import parse

    p = parse({"deployment": "fix", "executor": {"kind": "local"}, "work": "/w/nf", "data": "/d",
               "layout": {"layer2": "l2/{scan}/", "snapshots": ""}, "sources": ["gs://a"],
               "machines": {"big": {"type": "n2-standard-8", "cpus": 4, "memory": "8 GB"}}, "skip": ["digest", "static-names"],
               "options": {"list": {"import": True}}})
    assert H.executor_config(p).split("\n") == [
        "// Generated by `dt-cloud scan head` from the fix profile: not edited by hand.",
        "workDir = '/w/nf/work'",
        "process {",
        "    executor = 'local'",
        "    withLabel: big { cpus = 4; memory = '8 GB' }",
        "}",
        "",
    ]


def test_first_failed(tmp_path):
    t = tmp_path / "trace.tsv"
    t.write_text("task_id\thash\tnative_id\tname\tstatus\texit\n"
                 "1\tab/1\tj1\tindex\tCOMPLETED\t0\n"
                 "2\tcd/2\tj2\tpost\tFAILED\t1\n"
                 "3\tef/3\tj3\tinterval_store\tFAILED\t137\n")
    assert (H.first_failed(t), H.first_failed(tmp_path / "none.tsv")) == ("post (exit 1)", None)


def test_first_failed_skips_retried(tmp_path):
    """A fallback's queue timeout is a FAILED row followed by its retry's: the run's failure is the first one not retried."""
    t = tmp_path / "trace.tsv"
    t.write_text("task_id\thash\tnative_id\tname\tstatus\texit\n"
                 "1\tab/1\tj1\tindex\tFAILED\t-\n"
                 "2\tcd/2\tj2\tindex\tCOMPLETED\t0\n"
                 "3\tef/3\tj3\tinterval_store\tFAILED\t-\n"
                 "4\tgh/4\tj4\tinterval_store\tFAILED\t1\n")
    assert H.first_failed(t) == "interval_store (exit 1)"


def test_fallback_executor_config():
    """A label with fallbacks (spec §7): its machine by submit attempt, a queue timeout on every attempt but the last, and
    a retry only on Nextflow's submit timeout."""
    from dt_cloud.scan_profile import parse

    p = parse({"deployment": "fix", "executor": {"kind": "google-batch", "project": "p", "region": "r", "image": "img"},
               "work": "gs://w/nf", "data": "gs://w", "layout": {"layer2": "l2/{scan}/", "snapshots": ""}, "sources": ["gs://a"],
               "machines": {"big": {"type": "n2-highmem-32", "disk": {"request": 1500, "type": "local-ssd"}, "memory": "200 GB",
                                    "fallback": ["n2d-highmem-32", "n4-highmem-32"], "queue_timeout": 10},
                            "small": "e2-standard-2"},
               "skip": ["digest", "static-names"], "options": {"list": {"import": True}}})
    assert H.executor_config(p).split("\n") == [
        "// Generated by `dt-cloud scan head` from the fix profile: not edited by hand.",
        "workDir = 'gs://w/nf/work'",
        "plugins {",
        "    id 'nf-google@1.29.1'",
        "    id 'nf-cloudcache@0.6.2'",
        "}",
        "google {",
        "    project = 'p'",
        "    location = 'r'",
        "}",
        "process {",
        "    executor = 'google-batch'",
        "    container = 'img'",
        "    withLabel: big {",
        "        machineType = { ['n2-highmem-32', 'n2d-highmem-32', 'n4-highmem-32'][task.submitAttempt - 1] }",
        "        disk = [request: 1500.GB, type: 'local-ssd']",
        "        memory = '200 GB'",
        "        maxSubmitAwait = { task.submitAttempt < 3 ? '10m' : null }",
        "        maxRetries = 2",
        "        errorStrategy = {",
        "            if( !(task.previousException?.cause instanceof nextflow.exception.ProcessSubmitTimeoutException) ) return 'finish'",
        "            log.warn \"scan head: ${task.process} queued 10 min on ${task.machineType}; retrying on "
        "${['n2-highmem-32', 'n2d-highmem-32', 'n4-highmem-32'][task.submitAttempt]}"
        "${task.submitAttempt + 1 < 3 ? '' : ' (the last: no queue timeout)'}\"",
        "            return 'retry'",
        "        }",
        "    }",
        "    withLabel: small { machineType = 'e2-standard-2' }",
        "}",
        "",
    ]


def test_dry_run_writes_the_launch_files(tmp_path, monkeypatch):
    for k in ("SNAP_ID", "GEN", "DT_JOB_START", "DT_RUN_ID", "BATCH_JOB_UID", "SCAN_RUNS_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SNAP_ID", "2026-10-16T1200")
    monkeypatch.setenv("GEN", "G1")
    monkeypatch.setenv("DT_JOB_START", "100")
    launch = tmp_path / "launch"
    r = CliRunner().invoke(main, ["scan", "head", "-n", "-N", str(tmp_path / "nf"), "-w", str(launch), str(FIX / "cw-s3.yml")])
    assert r.exit_code == 0, r.output
    session = H.session_id("cw-s3", "2026-10-16T1200")
    assert sorted(p.name for p in launch.iterdir()) == [".nextflow", "executor.config", "params.json"]
    assert (launch / "executor.config").read_text() == (FIX / "cw-s3.executor.config").read_text()
    params = json.loads((launch / "params.json").read_text())
    assert (params["scan"], params["gen"], params["job_start"], params["session"]) == ("2026-10-16T1200", "G1", "100", session)
    assert json.loads(params["profile_json"])["deployment"] == "cw-s3"
    line = (launch / ".nextflow" / "history").read_text().split("\t")
    assert line[1:] == ["-", "scan_2026_10_16T1200", "OK", "-", session, "nextflow run scan.nf\n"]
    assert r.stderr.rstrip().split("\n")[-1] == (
        f"+ nextflow run {tmp_path}/nf/scan.nf -c {tmp_path}/nf/base.config -c {launch}/executor.config "
        f"-params-file {launch}/params.json -resume {session} -with-trace {launch}/trace.tsv "
        f"-with-dag {launch}/dag.mmd -with-timeline {launch}/timeline.html -with-report {launch}/report.html"
    )


def test_publish_reports(tmp_path):
    """The reports that exist go to `<work>/runs/<scan>/`; a missing one is skipped."""
    launch, work = tmp_path / "launch", tmp_path / "work"
    launch.mkdir()
    (launch / "dag.mmd").write_text("flowchart TB\n")
    (launch / "trace.tsv").write_text("task_id\n")
    profile = SimpleNamespace(work=str(work))
    assert H.publish_reports(profile, "2026-10-16T1200", launch) == ["runs/2026-10-16T1200/dag.mmd", "runs/2026-10-16T1200/trace.tsv"]
    assert sorted(p.relative_to(work).as_posix() for p in work.rglob("*") if p.is_file()) == [
        "runs/2026-10-16T1200/dag.mmd", "runs/2026-10-16T1200/trace.tsv"]
    assert (work / "runs/2026-10-16T1200/dag.mmd").read_text() == "flowchart TB\n"


def test_finish_args(tmp_path):
    """The DAG goes to `finish -D` only when Nextflow wrote it."""
    dag = tmp_path / "dag.mmd"
    assert H.finish_args(1, "nextflow exit 1", ["-l", "nf-session=s"], dag) == ["-x", "1", "-e", "nextflow exit 1", "-l", "nf-session=s"]
    dag.write_text("flowchart TB\n")
    assert H.finish_args(0, None, [], dag) == ["-x", "0", "-D", str(dag)]
