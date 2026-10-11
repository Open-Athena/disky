"""cw-s3's `job/scan-runs.json`: its static output follows the live generation in `job/static-names.json`."""
from __future__ import annotations

import json
from pathlib import Path

from dt_cloud.scan_runs import fill, load_profile

JOB = Path(__file__).parents[2] / "job"


def test_cw_profile_static_output_tracks_the_cw_generation(monkeypatch):
    monkeypatch.delenv("STATIC_NAMES_GEN", raising=False)
    [static] = [o for o in load_profile(JOB / "scan-runs.json").outputs if o.key == "static"]
    gen = json.loads((JOB / "static-names.json").read_text())["gen"]
    assert fill(static.uri, "2026-10-10T1201", None) == (
        f"gs://oa-gcs-usage-dvx/static-names/{gen}/manifests/2026-10-10T1201.json"
    )
