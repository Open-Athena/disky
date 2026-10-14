from pathlib import Path
from subprocess import run
from json import loads
from os import environ

import pytest

from dt_cloud.append_runner import pending_scans


@pytest.mark.parametrize("overrides, expected", [
    ({}, ["2026-10-12T0227", "2026-10-12"]),
    ({"SNAPSHOT_DATE": "2026-10-11"}, ["2026-10-11", "2026-10-11"]),
    ({"SNAP_ID": "2026-10-12T0345"}, ["2026-10-12T0345", "2026-10-12"]),
    ({"SNAP_ID": "2026-10-12T0345", "SNAPSHOT_DATE": "2026-10-11"}, ["2026-10-12T0345", "2026-10-12"]),
])
def test_job_default_has_a_timestamp_and_preserves_explicit_scan_ids(overrides, expected):
    script = (Path(__file__).resolve().parents[2] / "job/run.sh").read_text()
    assignments = [next(line for line in script.splitlines() if line.startswith(prefix)) for prefix in ["SNAP_ID=", "DATE="]]
    clock = '''
date() {
  case "$*" in
    '-u +%F') printf '2026-10-12\n' ;;
    '-u +%FT%H%M') printf '2026-10-12T0227\n' ;;
    *) return 22 ;;
  esac
}
'''
    result = run(["bash", "-c", clock + "\n".join(assignments) + '\nprintf "%s\\n" "$SNAP_ID" "$DATE"'], env={"PATH": "/usr/bin:/bin", **overrides}, capture_output=True, text=True)
    assert (result.returncode, result.stdout.splitlines(), result.stderr) == (0, expected, "")


def test_daily_scan_after_same_day_manual_scan_can_append():
    assert pending_scans(
        ["2026-10-11", "2026-10-12T0227"],
        ["2026-10-11", "2026-10-12T0227", "2026-10-12T0701"],
        "2026-10-12T0701",
        True,
    ) == ["2026-10-12T0701"]


@pytest.mark.parametrize("pin, expected", [
    ("", "registry@sha256:test"),
    ("1", "us-central1-docker.pkg.dev/oa-internal-450019/cloud-run-source-deploy/gcs-usage-snapshot:latest"),
])
def test_snapshot_image_is_forwarded_to_child_jobs(pin, expected):
    root = Path(__file__).resolve().parents[2]
    result = run(["bash", "job/batch-submit.sh"], cwd=root, env={
        "PATH": environ["PATH"], "DRY": "1", "PIN": pin, "IMAGE": "registry@sha256:test",
        "JOB_IMAGE": "unrelated-ambient-image",
    }, capture_output=True, text=True)
    assert (result.returncode, result.stderr) == (0, "")
    task = loads(result.stdout)["taskGroups"][0]["taskSpec"]
    assert (task["runnables"][0]["container"]["imageUri"], task["environment"]["variables"]["JOB_IMAGE"]) == (expected, expected)
