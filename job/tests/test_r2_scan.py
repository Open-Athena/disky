from datetime import datetime, timezone
from json import loads
from pathlib import Path
from runpy import run_path
from subprocess import CalledProcessError

import pytest


module = run_path(str(Path(__file__).resolve().parents[1] / "r2-scan"))
config = module["profile"]()
image = "us-west1-docker.pkg.dev/oa-internal-450019/oa-r2-usage/scan@sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def batch_identity(monkeypatch) -> None:
    monkeypatch.setenv("OA_R2_JOB_NAME", "oa-r2-scan-test")
    monkeypatch.setenv("OA_R2_IMAGE", image)


def test_profile_accepts_only_own_full_scans_and_immutable_archive() -> None:
    from dt_cloud.scan_runs import load_profile
    from dt_cloud.scan_runs_backfill import classify

    reporting = load_profile(Path(module["HERE"]) / "r2-scan-runs.json")
    def job(image_uri: str, entrypoint: str = "") -> dict:
        return {"name": "projects/p/locations/us-west1/jobs/job-generated", "taskGroups": [{"taskSpec": {"runnables": [{"container": {"imageUri": image_uri, "entrypoint": entrypoint}}]}}]}

    assert [classify(reporting, value) for value in [job(image), job(image, "python"), job(image.replace("oa-r2-usage/scan", "cw-s3-usage/scan"))]] == [reporting.jobs[0], None, None]
    assert (reporting.store, [(out.key, out.uri, out.after) for out in reporting.outputs]) == ("primary", [
        ("listing", "gs://oa-r2-usage-data/scans/{scan}/{gen}/listing/", "archive"),
        ("path-index", "gs://oa-r2-usage-data/scans/{scan}/{gen}/index/", "archive"),
        ("snapshot", "gs://oa-r2-usage-data/scans/{scan}/{gen}/snap/", "archive"),
    ])
    meta = job(image)
    meta["taskGroups"][0]["taskSpec"]["environment"] = {"variables": {"OA_R2_META_ONLY": "1"}}
    assert classify(reporting, meta) is None


def test_backfill_cli_delegates_to_installed_workspace_with_own_source(monkeypatch, tmp_path: Path) -> None:
    callback = module["backfill_runs"].callback
    namespace = callback.__globals__
    entry = tmp_path / "project/bin/dt-cloud"
    monkeypatch.setitem(namespace, "which", lambda name: str(entry))
    monkeypatch.setitem(namespace, "python_prefix", str(tmp_path / "standalone"))
    monkeypatch.setitem(namespace, "environ", {"PYTHONPATH": "original-source", "TEST_FLAG": "1"})
    calls = []
    monkeypatch.setitem(namespace, "run", lambda command, **kwargs: calls.append((command, kwargs)))
    callback(True, Path("tmp/inputs"))
    root = module["HERE"].parent
    assert calls == [(
        [str(entry.parent / "python"), str(module["HERE"] / "r2-scan"), "backfill-runs", "-n", "-w", "tmp/inputs"],
        {"env": {"PYTHONPATH": f"{root / 'src'}:{root / 'cloud/src'}:original-source", "TEST_FLAG": "1"}, "check": True},
    )]


def test_backfill_cli_stays_in_versioned_venv_with_symlinked_bin(monkeypatch, tmp_path: Path, capsys) -> None:
    from sys import stderr
    from dt_cloud import scan_runs_backfill

    root = tmp_path / "project/.venv"
    version = root / "3.13.7/bin"
    version.mkdir(parents=True)
    (root / "bin").symlink_to(version)
    callback = module["backfill_runs"].callback
    namespace = callback.__globals__
    monkeypatch.setitem(namespace, "which", lambda name: str(root / "bin/dt-cloud"))
    monkeypatch.setitem(namespace, "python_prefix", str(root))
    monkeypatch.setitem(namespace, "stderr", stderr)
    calls = []
    monkeypatch.setitem(namespace, "run", lambda command, **kwargs: calls.append((command, kwargs)))
    monkeypatch.setattr(scan_runs_backfill, "fetch_jobs", lambda project, regions: [])
    callback(True, None)
    assert calls == []
    assert capsys.readouterr().err == "No OA R2 full scan jobs found\n"


@pytest.mark.parametrize("fails", [False, True])
def test_live_reporter_keeps_phase_and_records_scan_exit(monkeypatch, fails: bool) -> None:
    from dt_cloud import scan_runs

    sink = scan_runs.SqliteSink(":memory:")
    monkeypatch.setattr(scan_runs, "D1Sink", lambda db: sink)
    monkeypatch.setenv("BATCH_JOB_UID", "own-uid")
    reporter_type = module["ScanReporter"]
    clock = iter([100, 100, 110, 120, 120, 125, 130])
    monkeypatch.setitem(reporter_type.__init__.__globals__, "time", lambda: next(clock))
    def execute() -> None:
        with reporter_type("2026-10-13T0716", "20261013T071607992442Z") as reporter:
            with reporter.phase("listing"):
                pass
            if fails:
                with reporter.phase("path-index"):
                    raise CalledProcessError(7, ["path-index", "private-value"])

    if fails:
        with pytest.raises(CalledProcessError):
            execute()
    else:
        execute()
    assert sink.query("SELECT run_id, scan, status, source, store, started_ts, finished_ts, failed_phase, error, gen FROM scan_runs") == [{
        "run_id": "own-uid", "scan": "2026-10-13T0716", "status": "failed" if fails else "succeeded", "source": "job", "store": "primary",
        "started_ts": 100, "finished_ts": 130 if fails else 125, "failed_phase": "path-index" if fails else None, "error": "CalledProcessError" if fails else None,
        "gen": "20261013T071607992442Z",
    }]
    assert sink.query("SELECT phase, seq, started_ts, finished_ts, status FROM scan_run_phases") == [{"phase": "listing", "seq": 0, "started_ts": 110, "finished_ts": 120, "status": "done"}]


def test_reporting_outage_does_not_fail_successful_scan(monkeypatch, capsys) -> None:
    from sys import stderr
    from dt_cloud import scan_runs

    def unavailable(db: str) -> None:
        raise ConnectionError("D1 unavailable")

    monkeypatch.setattr(scan_runs, "D1Sink", unavailable)
    monkeypatch.setenv("BATCH_JOB_UID", "own-uid")
    monkeypatch.setitem(module["ScanReporter"].write.__globals__, "stderr", stderr)
    with module["ScanReporter"]("2026-10-13T0716", "20261013T071607992442Z"):
        pass
    assert capsys.readouterr().err.splitlines() == [
        "OA-R2-SCAN-START 2026-10-13T0716 20261013T071607992442Z",
        "scan-run reporting failed (scan continues): ConnectionError: D1 unavailable",
        "scan-run reporting failed (scan continues): ConnectionError: D1 unavailable",
    ]


def test_scheduler_reporter_reads_local_task_group_metadata_without_batch_iam(monkeypatch) -> None:
    from io import BytesIO
    from dt_cloud import scan_runs
    from urllib import request

    sink = scan_runs.SqliteSink(":memory:")
    monkeypatch.setattr(scan_runs, "D1Sink", lambda db: sink)
    monkeypatch.setenv("BATCH_JOB_UID", "scheduled-uid")
    monkeypatch.delenv("OA_R2_JOB_NAME")
    calls = []

    def metadata(req, timeout):
        calls.append((req.full_url, req.get_header("Metadata-flavor"), timeout))
        return BytesIO(b"projects/965762293918/locations/us-west1/jobs/job-generated/taskGroups/group0")

    monkeypatch.setattr(request, "urlopen", metadata)
    with module["ScanReporter"]("2026-10-13T0716", "20261013T071607992442Z"):
        pass
    assert calls == [("http://metadata.google.internal/computeMetadata/v1/instance/attributes/cloudbatch-task-group", "Google", 5)]
    assert sink.query("SELECT job_name, region, machine, spot, tasks, image, status FROM scan_runs") == [{
        "job_name": "job-generated", "region": "us-west1", "machine": "e2-standard-8", "spot": 0, "tasks": 1,
        "image": "scan@sha256:" + "a" * 64, "status": "succeeded",
    }]


def test_remote_build_uses_only_own_identity_and_staging() -> None:
    assert module["build_command"](Path("context"), "first", config) == [
        "gcloud", "builds", "submit", "context",
        "--project", "oa-internal-450019", "--region", "us-west1",
        "--config", "context/job/cloudbuild.yaml",
        "--service-account", "projects/oa-internal-450019/serviceAccounts/oa-r2-usage-builder@oa-internal-450019.iam.gserviceaccount.com",
        "--gcs-source-staging-dir", "gs://oa-r2-usage-scratch/builds/source",
        "--polling-interval", "15",
        "--substitutions", "_IMAGE=us-west1-docker.pkg.dev/oa-internal-450019/oa-r2-usage/scan:first",
    ]


@pytest.mark.parametrize("size", [99, 101])
def test_publication_size_mismatch_refuses_pointer_update(size: int) -> None:
    class Publisher:
        def head_object(self, **kwargs) -> dict:
            assert kwargs == {"Bucket": "oa-r2-usage-index", "Key": "listing/first/path-index.parquet"}
            return {"ContentLength": size}

    with pytest.raises(ValueError, match=f"^Published object size mismatch: listing/first/path-index.parquet: {size} != 100$"):
        module["verify_uploads"](Publisher(), "oa-r2-usage-index", [("listing/first/path-index.parquet", 100)])


def test_job_uses_only_r2_resources_and_secret_containers() -> None:
    spec = module["job_spec"](image, config)
    assert spec["taskGroups"][0]["taskSpec"]["environment"] == {
        "variables": {
            "AWS_DEFAULT_REGION": "auto",
            "AWS_EC2_METADATA_DISABLED": "true",
            "CLOUDFLARE_ACCOUNT_ID": "74981a43be0de7712369306c7b19133d",
            "D1_DB_ID": "3bd87725-4f09-4316-877c-d089563a992e",
            "D1_DB_NAME": "oa-r2-usage-db",
            "DISK_TREE_R2_ENDPOINT_URL": "https://74981a43be0de7712369306c7b19133d.r2.cloudflarestorage.com",
            "DUCKDB_MEM": "12GB",
            "DUCKDB_THREADS": "8",
                "OA_R2_IMAGE": image,
            "DISKY_LABELS": "app=disky,deployment=oa-r2,component=scan",
        },
        "secretVariables": {
            "SOURCE_ACCESS_KEY_ID": "projects/oa-internal-450019/secrets/oa-r2-source-access-key-id/versions/latest",
            "SOURCE_SECRET_ACCESS_KEY": "projects/oa-internal-450019/secrets/oa-r2-source-secret-access-key/versions/latest",
            "R2_ACCESS_KEY_ID": "projects/oa-internal-450019/secrets/oa-r2-index-access-key-id/versions/latest",
            "R2_SECRET_ACCESS_KEY": "projects/oa-internal-450019/secrets/oa-r2-index-secret-access-key/versions/latest",
            "CLOUDFLARE_API_TOKEN": "projects/oa-internal-450019/secrets/oa-r2-d1-token/versions/latest",
        },
    }
    assert spec["allocationPolicy"]["serviceAccount"] == {"email": "oa-r2-usage-job@oa-internal-450019.iam.gserviceaccount.com"}
    assert spec["taskGroups"][0]["taskSpec"]["runnables"] == [{"container": {
        "imageUri": image, "volumes": ["/mnt/disks/r2-stage:/stage"],
    }}]
    assert (spec["taskGroups"][0]["taskSpec"]["maxRetryCount"], spec["taskGroups"][0]["taskSpec"]["maxRunDuration"]) == (0, "7200s")


@pytest.mark.parametrize("value", [
    image.replace("oa-r2-usage/scan", "cw-s3-usage/scan"),
    image.replace("docker.pkg.dev", "dockerXpkgXdev"),
    image.split("@")[0] + ":latest",
])
def test_image_must_be_immutable_and_deployment_owned(value: str) -> None:
    with pytest.raises(ValueError, match="^Image must be pinned by digest in this deployment's scan repository$"):
        module["job_spec"](value, config)


@pytest.fixture
def scan(tmp_path: Path) -> Path:
    from pyarrow import Table
    from pyarrow.parquet import write_table
    from dt_cloud.viz import write_path_index

    for index, bucket in enumerate(config["buckets"], 1):
        directory = tmp_path / "listing" / bucket
        directory.mkdir(parents=True)
        write_table(Table.from_pylist([{
            "bucket": bucket, "name": "folder/object", "size_bytes": index * 100,
            "created": datetime(2026, 10, 11, tzinfo=timezone.utc), "storage_class_id": 1,
        }]), directory / "shard-0.parquet")
    write_path_index(
        (str(tmp_path / "listing/*/*.parquet"),), tmp_path / "snap", "2026-10-12T1200",
        path_index=tmp_path / "index/path-index.parquet", user_sorts=False, search=True,
    )
    module["build_age"](tmp_path / "index/path-index.parquet", tmp_path / "index")
    return tmp_path


def test_real_listing_build_and_verification_agree_exactly(scan: Path) -> None:
    assert module["verify_scan"](scan, config, "2026-10-12T1200") == {
        "scan": "2026-10-12T1200", "date_basis": "last_modified",
        "buckets": {
            "marin-na": {"bytes": 100, "objects": 1},
            "marin-discord": {"bytes": 200, "objects": 1},
            "oa-cw-s3-usage-index": {"bytes": 300, "objects": 1},
            "oa-gcs-usage-index": {"bytes": 400, "objects": 1},
        },
        "totals": {"bytes": 1000, "objects": 4},
    }
    assert sorted(path.name for path in (scan / "index").glob("path-index.*.parquet")) == [
        "path-index.groups.parquet",
        "path-index.rows-search.parquet", "path-index.rows.parquet", "path-index.trigrams.parquet",
    ]


def test_histogram_shortfall_refuses_publication(scan: Path) -> None:
    path = scan / "snap/age.json"
    rows = loads(path.read_text())
    rows[0]["b"] += 1
    from json import dumps

    path.write_text(dumps(rows) + "\n")
    with pytest.raises(ValueError, match="^Last-modified histogram does not match the dated listing population$"):
        module["verify_scan"](scan, config, "2026-10-12T1200")


def test_all_age_tiers_preserve_exact_root_and_bucket_populations(scan: Path) -> None:
    from duckdb import connect
    from dt_cloud.index import AGE_PYRAMID_BINS

    expected = [
        ("", 0, 1000, 4),
        ("marin-discord", 1, 200, 1),
        ("marin-na", 1, 100, 1),
        ("oa-cw-s3-usage-index", 1, 300, 1),
        ("oa-gcs-usage-index", 1, 400, 1),
    ]
    with connect() as con:
        actual = {
            bin: con.execute("SELECT path, depth, sum(b)::BIGINT, sum(o)::BIGINT FROM read_parquet(?) WHERE depth <= 1 GROUP BY path, depth ORDER BY depth, path", [str(scan / f"index/age-pyramid-{bin}.parquet")]).fetchall()
            for bin in AGE_PYRAMID_BINS
        }
    assert actual == {bin: expected for bin in ("1h", "3h", "6h", "12h", "1d", "2d", "4d", "8d")}


def test_corrupt_age_tier_refuses_publication(scan: Path) -> None:
    from pyarrow import array
    from pyarrow.parquet import read_table, write_table

    tier = scan / "index/age-pyramid-1h.parquet"
    table = read_table(tier)
    table = table.set_column(table.schema.get_field_index("b"), "b", array([value + 1 for value in table["b"].to_pylist()]))
    write_table(table, tier)
    with pytest.raises(ValueError, match="^Age pyramid 1h does not match the dated path population$"):
        module["verify_scan"](scan, config, "2026-10-12T1200")


def test_index_stages_authenticate_with_the_serving_key_not_the_source_key(monkeypatch) -> None:
    namespace = module["index_env"].__globals__
    monkeypatch.setitem(namespace, "environ", {
        "AWS_ACCESS_KEY_ID": "source-id", "AWS_SECRET_ACCESS_KEY": "source-secret",
        "R2_ACCESS_KEY_ID": "index-id\n", "R2_SECRET_ACCESS_KEY": "index-secret\n", "TEST_FLAG": "1",
    })
    assert module["index_env"]() == {
        "AWS_ACCESS_KEY_ID": "index-id", "AWS_SECRET_ACCESS_KEY": "index-secret",
        "R2_ACCESS_KEY_ID": "index-id\n", "R2_SECRET_ACCESS_KEY": "index-secret\n", "TEST_FLAG": "1",
    }
