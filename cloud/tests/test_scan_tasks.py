"""Opt-in split workers preserve publication, immutable config and attribution."""
from copy import deepcopy
from json import loads
from pathlib import Path
from subprocess import run
from os import environ

import pytest
from click.testing import CliRunner
from requests import HTTPError, Response

from dt_cloud.scan_tasks import SHAPES, compatible, task_cmd, task_name, task_spec
from dt_cloud.scan_runs import load_profile
from dt_cloud.scan_runs_backfill import classify, reconstruct

IMAGE = "registry/scan@sha256:" + "a" * 64
SCAN = "2026-10-13T1300"
ROOT_DIR = Path(__file__).resolve().parents[2]
CONTEXT = {"JOB_IMAGE": IMAGE, "DATA_BUCKET": "data", "GEN": "20261013T130000Z", "SNAP_PATH": "snapshots/" + SCAN,
           "INDEX_PATH": "listing/" + SCAN + "/index/20261013T130000Z/path-index.parquet"}


def controller() -> dict:
    return {
        "uid": "root-uid", "name": "projects/p/locations/us-central1/jobs/root",
        "createTime": "2026-10-13T13:00:00Z", "status": {"state": "RUNNING"},
        "taskGroups": [{"taskCount": 1, "taskSpec": {
            "runnables": [{"container": {"imageUri": IMAGE, "volumes": ["/mnt/disks/stage:/stage:rw"]}}],
            "environment": {
                "variables": {"DISKY_LABELS": "app=disky,deployment=gcs", "SNAP_ID": SCAN,
                              "SPLIT_COMPUTE": "1", "DUCKDB_MEM": "8GB", "DUCKDB_THREADS": "4"},
                "secretVariables": {"TOKEN": "projects/p/secrets/token/versions/1"},
            },
            "volumes": [{"deviceName": "stage", "mountPath": "/mnt/disks/stage"}],
        }}],
        "allocationPolicy": {"serviceAccount": {"email": "job@p"}, "location": {"allowedLocations": ["regions/us-central1"]}},
    }


@pytest.mark.parametrize("stage, expected", [
    ("compute", {"machine": "n2-highmem-32", "memory_mib": 250000, "ssd_gb": 1500, "duckdb_mem": "100GB", "threads": "16"}),
    ("access", {"machine": "n2-highmem-16", "memory_mib": 123200, "ssd_gb": 750, "duckdb_mem": "48GB", "threads": "16"}),
])
def test_worker_shape_context_and_secret_references(stage, expected):
    root = controller()
    original = deepcopy(root)
    out = task_spec(root, stage, SCAN, {**CONTEXT, "TOKEN": "resolved-secret-not-to-copy", "OTHER": "not-config"})
    assert root == original
    assert SHAPES[stage] == expected
    task = out["taskGroups"][0]["taskSpec"]
    assert task["environment"]["secretVariables"] == {"TOKEN": "projects/p/secrets/token/versions/1"}
    assert task["environment"]["variables"] == {
        "DISKY_LABELS": "app=disky,deployment=gcs", "SNAP_ID": SCAN, "SCAN_ID": SCAN,
        "SPLIT_COMPUTE": "0", "SCAN_RUN_ID": "root-uid", "SCAN_ROOT_UID": "root-uid",
        "DUCKDB_MEM": expected["duckdb_mem"], "DUCKDB_THREADS": "16", "STAGE_DIR": "/stage",
        **CONTEXT,
        **({"SCAN_COMPUTE_ONLY": "1", "REPROC": "1", "SKIP_ACCESS": "1"} if stage == "compute" else {"ACCESS_ONLY": "1", "DUCKDB_MEM_ACCESS": "48GB", "DUCKDB_THREADS_ACCESS": "16"}),
    }
    assert task["computeResource"] == {"cpuMilli": 32000 if stage == "compute" else 16000, "memoryMib": expected["memory_mib"]}
    assert (task["maxRetryCount"], task["maxRunDuration"]) == (0, "21600s")
    assert task["runnables"] == [{"container": {"imageUri": IMAGE, "entrypoint": "bash", "commands": ["job/run.sh"], "volumes": ["/mnt/disks/stage:/stage:rw"]}}]
    assert out["allocationPolicy"]["instances"] == [{"policy": {
        "machineType": expected["machine"], "provisioningModel": "STANDARD",
        "bootDisk": {"type": "pd-balanced", "sizeGb": "100"},
        "disks": [{"newDisk": {"type": "local-ssd", "sizeGb": str(expected["ssd_gb"])}, "deviceName": "stage"}],
    }}]
    labels = {"app": "disky", "deployment": "gcs", "component": "scan-compute" if stage == "compute" else "access",
              "scan_run": "root-uid", "scan_id": "2026-10-13t1300", "stage": stage}
    assert out["allocationPolicy"]["labels"] == labels
    assert {k: v for k, v in out["labels"].items() if k != "task_config"} == {**labels, "purpose": "scan-pipeline"}
    assert len(out["labels"]["task_config"]) == 32
    assert task["volumes"] == original["taskGroups"][0]["taskSpec"]["volumes"]


@pytest.mark.parametrize("image", ["registry:latest", "registry@sha256:x", "registry@sha256:" + "a" * 63])
def test_bad_image_refused(image):
    with pytest.raises(ValueError, match="^split scan requires a digest-pinned JOB_IMAGE$"):
        task_spec(controller(), "compute", SCAN, {**CONTEXT, "JOB_IMAGE": image})


def test_worker_cannot_change_controller_image():
    with pytest.raises(ValueError, match="^worker image must match the controller's pinned image$"):
        task_spec(controller(), "compute", SCAN, {**CONTEXT, "JOB_IMAGE": "registry/scan@sha256:" + "b" * 64})


@pytest.mark.parametrize("missing", ["GEN", "SNAP_PATH", "INDEX_PATH"])
def test_missing_publication_identity_refused(missing):
    with pytest.raises(ValueError, match=f"^compute task needs {missing}$"):
        task_spec(controller(), "compute", SCAN, {k: v for k, v in CONTEXT.items() if k != missing})


def test_sweep_controller_refused():
    root = controller()
    root["taskGroups"][0]["taskSpec"]["environment"]["variables"]["SWEEP"] = "real"
    with pytest.raises(ValueError, match="^split tasks require a scan controller, not another job mode$"):
        task_spec(root, "compute", SCAN, CONTEXT)


def test_idempotent_name_and_immutable_config():
    assert [task_name("root-uid", stage, scan) for stage, scan in [("compute", SCAN), ("access", SCAN), ("compute", "2026-10-13T1400")]] == [
        "sc-compute-2026-10-13-1300-dc437d2b6821380d",
        "sc-access-2026-10-13-1300-dc437d2b6821380d",
        "sc-compute-2026-10-13-1400-dc437d2b6821380d",
    ]
    expected = task_spec(controller(), "compute", SCAN, CONTEXT)
    actual = deepcopy(expected)
    actual.update(uid="child-uid", status={"state": "RUNNING"})
    actual["taskGroups"][0]["name"] = "api-group-name"
    actual["taskGroups"][0]["taskSpec"].pop("maxRetryCount")
    actual["taskGroups"][0]["taskSpec"]["computeResource"]["memoryMib"] = "250000"
    assert compatible(actual, expected) is True
    actual["taskGroups"][0]["taskSpec"]["environment"]["variables"]["GATE"] = "1"
    assert compatible(actual, expected) is False
    actual = deepcopy(expected)
    actual["allocationPolicy"]["instances"][0]["policy"]["machineType"] = "n2-highmem-16"
    assert compatible(actual, expected) is False


@pytest.mark.parametrize("mode, state, expected_code", [("dry", "SUCCEEDED", 0), ("new", "SUCCEEDED", 0), ("resume", "SUCCEEDED", 0), ("resume", "FAILED", 1)])
def test_cli_submit_resume_and_failure(monkeypatch, mode, state, expected_code):
    from dt_cloud import batch, gcp

    root = controller()
    for k, v in {**CONTEXT, "BATCH_JOB_UID": "root-uid"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(gcp, "batch_jobs", lambda *_: [root])
    monkeypatch.setattr(gcp, "gcp_project", lambda: "p")
    spec = task_spec(root, "compute", SCAN, CONTEXT)
    name = task_name("root-uid", "compute", SCAN)
    calls = []
    logs = []
    monkeypatch.setattr("dt_cloud.static_names.err", logs.append)

    def existing(*args, **kwargs):
        if mode == "new":
            response = Response()
            response.status_code = 404
            raise HTTPError(response=response)
        return spec

    monkeypatch.setattr(gcp, "batch_job", existing)
    monkeypatch.setattr(batch, "submit_job", lambda s, n, r: calls.append(("submit", s, n, r)))

    def wait(jobs, interval, log):
        calls.append(("wait", jobs, interval))
        return {name: state}

    monkeypatch.setattr(batch, "wait_jobs", wait)
    result = CliRunner().invoke(task_cmd, ["-s", "compute", *(["-n"] if mode == "dry" else []), SCAN])
    assert result.exit_code == expected_code
    if mode == "dry":
        assert loads(result.output) == {"job": name, "shape": SHAPES["compute"], "labels": spec["labels"]}
        assert calls == []
    else:
        assert calls == ([("submit", spec, name, "us-central1")] if mode == "new" else []) + [("wait", [(name, "us-central1")], 15)]
        assert logs == [f"scan task {'submitted' if mode == 'new' else 'resumed'}: {name} [us-central1]"]
        assert result.output.splitlines() == ([f"{name}: FAILED"] if state == "FAILED" else [])


@pytest.mark.parametrize("pin, expected", [("", ("n2-standard-4", 4000, 14000, "8GB", "4", "1")), ("1", ("n2-highmem-32", 8000, 250000, "100GB", "16", None))])
def test_split_is_manual_only_and_does_not_change_cron(pin, expected):
    result = run(["bash", "job/batch-submit.sh"], cwd=ROOT_DIR, env={
        "PATH": environ["PATH"], "DRY": "1", "PIN": pin, "SPLIT_COMPUTE": "1", "IMAGE": IMAGE,
    }, capture_output=True, text=True)
    assert (result.returncode, result.stderr) == (0, "")
    spec = loads(result.stdout)
    task = spec["taskGroups"][0]["taskSpec"]
    env = task["environment"]["variables"]
    assert (spec["allocationPolicy"]["instances"][0]["policy"]["machineType"], task["computeResource"]["cpuMilli"],
            task["computeResource"]["memoryMib"], env["DUCKDB_MEM"], env["DUCKDB_THREADS"], env.get("SPLIT_COMPUTE")) == expected
    assert spec["allocationPolicy"]["instances"][0]["policy"]["disks"] == ([] if not pin else [{"newDisk": {"type": "local-ssd", "sizeGb": "1500"}, "deviceName": "stage"}])
    assert [v for v in task["volumes"] if v.get("deviceName") == "stage"] == ([] if not pin else [{"deviceName": "stage", "mountPath": "/mnt/disks/stage"}])
    assert env["STAGE_DIR"] == ("/tmp" if not pin else "/stage")


def test_downstream_backfill_retains_explicit_parent():
    profile = load_profile(ROOT_DIR / "job/scan-runs.json")
    jobs = []
    for stage in ("compute", "access"):
        spec = task_spec(controller(), stage, SCAN, CONTEXT)
        spec.update(uid=stage, name=f"projects/p/locations/us-central1/jobs/{stage}", createTime="2026-10-13T13:20:00Z", status={"state": "RUNNING"})
        jobs.append(spec)
    assert [classify(profile, job).kind for job in jobs] == ["compute", "access"]
    assert [(r.run.run_id, r.run.parent, r.run.kind) for r in reconstruct(profile, jobs, [], None, now=1791900000)] == [
        ("access", "root-uid", "access"), ("compute", "root-uid", "compute"),
    ]


@pytest.mark.parametrize("code, scratch, expected", [
    (0, "0", (0, ["START scan-compute", "WORKER compute s g snapshots/s listing/s/index/g/path-index.parquet", "END scan-compute", "POST-PUBLISH"])),
    (7, "0", (7, ["START scan-compute", "WORKER compute s g snapshots/s listing/s/index/g/path-index.parquet"])),
    (0, "1", (0, ["START scan-compute", "WORKER compute s g snapshots/s listing/s/index/g/path-index.parquet", "END scan-compute"])),
])
def test_controller_waits_for_compute_and_scratch_never_advances_pointers(code, scratch, expected):
    script = (ROOT_DIR / "job/run.sh").read_text()
    start = script.index('if [ "${SPLIT_COMPUTE:-0}" = "1" ]; then\n  begin_phase scan-compute')
    block = script[start:].split("else\nbegin_phase path-index", 1)[0] + "fi\n"
    preamble = r'''
set -eu
SPLIT_COMPUTE=1 SNAP_ID=s GEN=g SNAP_PATH=snapshots/s INDEX_PATH=listing/s/index/g/path-index.parquet
begin_phase() { printf 'START %s\n' "$*"; }
phase() { printf 'END %s\n' "$*"; }
dt-cloud() { printf 'WORKER %s %s %s %s %s\n' "$4" "$5" "$GEN" "$SNAP_PATH" "$INDEX_PATH"; return "$CODE"; }
'''
    result = run(["bash", "-c", preamble + block + "printf 'POST-PUBLISH\\n'"], env={"PATH": "/usr/bin:/bin", "CODE": str(code), "SCRATCH": scratch}, capture_output=True, text=True)
    assert (result.returncode, result.stdout.splitlines()) == expected
    assert result.stderr == ("SCRATCH — split computation complete; no serving pointers changed\n" if scratch == "1" and code == 0 else "")


@pytest.mark.parametrize("overrides, expected", [
    ({}, (0, "")),
    ({"JOB_IMAGE": "registry:latest"}, (1, "ERROR: split scan requires a digest-pinned JOB_IMAGE\n")),
    ({"GATE": "1"}, (1, "ERROR: split scan does not support GATE\n")),
    ({"SCRATCH": "1"}, (1, "ERROR: split SCRATCH requires SNAP_PATH and INDEX_PATH under scratch/\n")),
    ({"SCRATCH": "1", "SNAP_PATH": "scratch/check/snapshot", "INDEX_PATH": "scratch/check/index/path-index.parquet"}, (0, "")),
])
def test_controller_preflight(overrides, expected):
    script = (ROOT_DIR / "job/run.sh").read_text()
    block = script.split('INDEX_PATH=${INDEX_PATH:-$INDEX_DIR/path-index.parquet}\n', 1)[1].split("# Scan-run record", 1)[0]
    result = run(["bash", "-c", "set -eu\n" + block], env={
        "PATH": "/usr/bin:/bin", "SPLIT_COMPUTE": "1", "JOB_IMAGE": IMAGE, "BATCH_JOB_UID": "root",
        "SNAP_PATH": CONTEXT["SNAP_PATH"], "INDEX_PATH": CONTEXT["INDEX_PATH"], **overrides,
    }, capture_output=True, text=True)
    assert (result.returncode, result.stderr) == expected
    assert result.stdout == ""


def test_split_bad_image_refused_before_allocating_a_vm():
    result = run(["bash", "job/batch-submit.sh"], cwd=ROOT_DIR, env={"PATH": environ["PATH"], "SPLIT_COMPUTE": "1", "IMAGE": "registry:latest"}, capture_output=True, text=True)
    assert (result.returncode, result.stdout, result.stderr) == (1, "", "ERROR: split scan requires a digest-pinned IMAGE\n")


@pytest.mark.parametrize("pin, expected", [("", ["zones/us-central1-f"]), ("1", ["regions/us-central1"])])
def test_manual_zone_override_does_not_change_cron(pin, expected):
    result = run(["bash", "job/batch-submit.sh"], cwd=ROOT_DIR, env={"PATH": environ["PATH"], "DRY": "1", "PIN": pin, "ZONE": "us-central1-f"}, capture_output=True, text=True)
    assert (result.returncode, result.stderr) == (0, "")
    assert loads(result.stdout)["allocationPolicy"]["location"] == {"allowedLocations": expected}


def test_worker_owns_ssd_mount_even_with_a_boot_disk_only_controller():
    root = controller()
    source = root["taskGroups"][0]["taskSpec"]
    source["volumes"] = [{"gcs": {"remotePath": "data"}, "mountPath": "/mnt/disks/gcs/data"}]
    source["runnables"][0]["container"]["volumes"] = ["/mnt/disks/gcs/data:/gcs/data:rw"]
    task = task_spec(root, "compute", SCAN, CONTEXT)["taskGroups"][0]["taskSpec"]
    assert task["volumes"] == [
        {"gcs": {"remotePath": "data"}, "mountPath": "/mnt/disks/gcs/data"},
        {"deviceName": "stage", "mountPath": "/mnt/disks/stage"},
    ]
    assert task["runnables"][0]["container"]["volumes"] == ["/mnt/disks/gcs/data:/gcs/data:rw", "/mnt/disks/stage:/stage:rw"]


def test_four_core_controller_override_keeps_a_satisfiable_request():
    result = run(["bash", "job/batch-submit.sh"], cwd=ROOT_DIR, env={"PATH": environ["PATH"], "DRY": "1", "MACHINE": "e2-standard-4", "SPLIT_COMPUTE": "1", "IMAGE": IMAGE}, capture_output=True, text=True)
    assert (result.returncode, result.stderr) == (0, "")
    spec = loads(result.stdout)
    assert spec["taskGroups"][0]["taskSpec"]["computeResource"] == {"cpuMilli": 4000, "memoryMib": 14000}
    assert spec["allocationPolicy"]["instances"][0]["policy"]["machineType"] == "e2-standard-4"


@pytest.mark.parametrize("mode, expected_post", [({}, True), ({"SCRATCH": "1"}, False), ({"GATE": "1"}, False)])
def test_validation_failure_stays_local_but_preserves_exit_and_error(mode, expected_post):
    script = (ROOT_DIR / "job/run.sh").read_text()
    functions = "slack_post() {" + script.split("slack_post() {", 1)[1].split("trap 'fail_alert", 1)[0]
    preamble = r'''
set -e
SNAP_ID=s BATCH_JOB_UID=root
python3() { printf 'POST %s\n' "$SLACK_MSG"; }
'''
    invocation = r'''
trap 'fail_alert $? 300 "worker"' ERR
trap 'printf "RECORDED %s\n" "$SR_ERR"' EXIT
worker() { return 7; }
worker
printf 'UNEXPECTED SUCCESS\n'
'''
    result = run(["bash", "-c", preamble + functions + invocation], env={"PATH": "/usr/bin:/bin", **mode}, capture_output=True, text=True)
    message = "❌ `dt-cloud` snapshot job failed (s): `worker` exited 7 at run.sh:300\n<https://console.cloud.google.com/logs/query;query=labels.job_uid%3D%22root%22?project=oa-internal-450019|task logs>"
    assert result.returncode == 7
    assert result.stdout == (f"POST {message}\n" if expected_post else "") + "RECORDED exit 7: worker (run.sh:300)\n"
    assert result.stderr == ("" if expected_post else f"Validation alert (Slack suppressed): {message}\n")
