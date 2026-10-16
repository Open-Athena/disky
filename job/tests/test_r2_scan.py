from datetime import datetime, timezone
from json import loads
from pathlib import Path
from runpy import run_path
from subprocess import CalledProcessError
import sys

import pytest


module = run_path(str(Path(__file__).resolve().parents[1] / "r2-scan"))
config = module["profile"]()
image = "us-west1-docker.pkg.dev/oa-internal-450019/oa-r2-usage/scan@sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def batch_identity(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OA_R2_JOB_NAME", "oa-r2-scan-test")
    monkeypatch.setenv("OA_R2_IMAGE", image)
    # the shared job wrapper's state files (`$TMPDIR/dt-scan-run/`) and the PHASE lines' clock
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("DT_JOB_START", "100")
    for name in ("DT_SUPERVISE", "SCAN_ID"):
        monkeypatch.delenv(name, raising=False)
    # The shared wrapper's `err` is bound to the `sys.stderr` of whichever test imported it first (a capture since closed)
    from dt_cloud import scan_runs

    monkeypatch.setattr(scan_runs, "err", lambda *a: print(*a, file=sys.stderr))


def clock(monkeypatch) -> list[int]:
    """The shared job wrapper's clock (`scan_runs._now`), set by the test between steps."""
    from dt_cloud import scan_runs

    now = [100]
    monkeypatch.setattr(scan_runs, "_now", lambda: now[0])
    return now


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
    monkeypatch.setenv("DT_ALERTS", "stderr")
    now = clock(monkeypatch)
    reporter_type = module["ScanReporter"]
    def execute() -> None:
        with reporter_type("2026-10-13T0716", "20261013T071607992442Z") as reporter:
            now[0] = 110
            with reporter.phase("listing"):
                now[0] = 120
            if fails:
                with reporter.phase("path-index"):
                    now[0] = 130
                    raise CalledProcessError(7, ["path-index", "private-value"])
            now[0] = 125

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
    assert sink.query("SELECT phase, seq, started_ts, finished_ts, status FROM scan_run_phases") == [
        {"phase": "listing", "seq": 0, "started_ts": 110, "finished_ts": 120, "status": "done"},
        *([{"phase": "path-index", "seq": 1, "started_ts": 120, "finished_ts": 130, "status": "failed"}] if fails else []),
    ]


def test_reporting_outage_does_not_fail_successful_scan(monkeypatch, capsys) -> None:
    from dt_cloud import scan_runs

    def unavailable(db: str) -> None:
        raise ConnectionError("D1 unavailable")

    monkeypatch.setattr(scan_runs, "D1Sink", unavailable)
    monkeypatch.setenv("BATCH_JOB_UID", "own-uid")
    monkeypatch.setitem(module["ScanReporter"].__enter__.__globals__, "stderr", sys.stderr)
    clock(monkeypatch)
    with module["ScanReporter"]("2026-10-13T0716", "20261013T071607992442Z"):
        pass
    assert capsys.readouterr().err.splitlines() == [
        "OA-R2-SCAN-START 2026-10-13T0716 20261013T071607992442Z",
        "scan-run record failed (the scan continues): ConnectionError: D1 unavailable",
        "scan-run record failed (the scan continues): ConnectionError: D1 unavailable",
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
            "SITE_URL": "https://r2.oa.dev",
            "SLACK_CHANNEL": "C0C9J6CE8BD",
            "SLACK_ALERT_CHANNEL": "C0C9U47KTM2",
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


def test_optional_secrets_reach_the_job_only_once_live() -> None:
    live = {**config, "optional_secrets_live": ["SLACK_BOT_TOKEN", "SITE_TOKEN"], "slack_channel": "C0R2USAGE", "slack_alert_channel": "C0R2ALERTS"}
    env = module["job_spec"](image, live)["taskGroups"][0]["taskSpec"]["environment"]
    assert ({k: env["variables"][k] for k in ("SLACK_CHANNEL", "SLACK_ALERT_CHANNEL")}, sorted(env["secretVariables"].items())[:3]) == (
        {"SLACK_CHANNEL": "C0R2USAGE", "SLACK_ALERT_CHANNEL": "C0R2ALERTS"},
        [
            ("CLOUDFLARE_API_TOKEN", "projects/oa-internal-450019/secrets/oa-r2-d1-token/versions/latest"),
            ("R2_ACCESS_KEY_ID", "projects/oa-internal-450019/secrets/oa-r2-index-access-key-id/versions/latest"),
            ("R2_SECRET_ACCESS_KEY", "projects/oa-internal-450019/secrets/oa-r2-index-secret-access-key/versions/latest"),
        ],
    )
    assert {k: v for k, v in env["secretVariables"].items() if k in config["optional_secrets"]} == {
        "SLACK_BOT_TOKEN": "projects/oa-internal-450019/secrets/oa-r2-slack-bot-token/versions/latest",
        "SITE_TOKEN": "projects/oa-internal-450019/secrets/oa-r2-site-grant/versions/latest",
    }
    with pytest.raises(ValueError, match=r"^optional_secrets_live names no optional secret: \['HF_TOKEN'\]$"):
        module["job_spec"](image, {**config, "optional_secrets_live": ["HF_TOKEN"]})


def slack_posts(monkeypatch) -> list[dict]:
    from disk_tree.notify import push

    posts = []
    def post(url: str, body: bytes, headers: dict) -> tuple[int, str]:
        posts.append({"url": url, "auth": headers["Authorization"], **loads(body)})
        return 200, '{"ok": true}'
    monkeypatch.setattr(push, "http_post", post)
    return posts


def test_failed_run_alerts_the_alert_channel_once_naming_its_phase(monkeypatch, tmp_path: Path) -> None:
    from dt_cloud import scan_runs

    monkeypatch.setattr(scan_runs, "D1Sink", lambda db: scan_runs.SqliteSink(":memory:"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("BATCH_JOB_UID", "own-uid")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setenv("SLACK_ALERT_CHANNEL", "C0R2ALERTS")
    monkeypatch.delenv("DISK_TREE_NOTIFY", raising=False)
    monkeypatch.delenv("DT_ALERTS", raising=False)
    posts = slack_posts(monkeypatch)
    with pytest.raises(CalledProcessError):
        with module["ScanReporter"]("2026-10-13T0716", "20261013T071607992442Z") as reporter:
            with reporter.phase("listing"):
                pass
            with reporter.phase("path-index"):
                raise CalledProcessError(7, ["path-index", "xoxb-secret"])
    assert posts == [{
        "url": "https://slack.com/api/chat.postMessage", "auth": "Bearer xoxb-secret", "channel": "C0R2ALERTS",
        "text": "*❌ OA R2 scan 2026-10-13T0716*\npath-index failed (exit 7): CalledProcessError\n"
                "<https://console.cloud.google.com/logs/query;query=labels.job_uid%3D%22own-uid%22?project=oa-internal-450019|task logs>",
        "unfurl_links": False, "unfurl_media": False,
    }]


def test_without_an_alert_channel_a_failure_posts_nothing(monkeypatch, tmp_path: Path) -> None:
    from dt_cloud import scan_runs

    monkeypatch.setattr(scan_runs, "D1Sink", lambda db: scan_runs.SqliteSink(":memory:"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("BATCH_JOB_UID", "own-uid")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    for name in ("SLACK_ALERT_CHANNEL", "DISK_TREE_NOTIFY", "DT_ALERTS", "PUSHOVER_TOKEN", "DISCORD_NOTIFY_WEBHOOK"):
        monkeypatch.delenv(name, raising=False)
    posts = slack_posts(monkeypatch)
    with pytest.raises(RuntimeError):
        with module["ScanReporter"]("2026-10-13T0716", "20261013T071607992442Z"):
            raise RuntimeError("boom")
    assert posts == []


SERVE_SECRETS = ("SITE_TOKEN", "SLACK_BOT_TOKEN", "SLACK_CHANNEL", "CF_PAGES_TOKEN")


def serve_names(monkeypatch, **env: str) -> list[tuple[str, list[str], str | None]]:
    namespace = module["serve_steps"].__globals__
    monkeypatch.setitem(namespace, "environ", {"R2_ACCESS_KEY_ID": "index-id", "R2_SECRET_ACCESS_KEY": "index-secret", **env})
    return [(name, command, step_env.get("CLOUDFLARE_API_TOKEN")) for name, command, step_env, _ in module["serve_steps"](config, "2026-10-16T0715")]


def test_serving_steps_are_skipped_without_their_credentials(monkeypatch) -> None:
    assert serve_names(monkeypatch) == []
    assert serve_names(monkeypatch, SITE_TOKEN="grant", SLACK_BOT_TOKEN="xoxb", SLACK_CHANNEL="C0R2USAGE") == [
        ("warm-cache", ["dt-cloud", "warm-cache", "-d", "2026-10-16T0715", "-r", "r2://oa-r2-usage-index/snapshots/r2"], None),
        ("probe", ["dt-cloud", "probe", "-c", "-s", "r2", "-u", "https://r2.oa.dev", "-o", "r2://oa-r2-usage-index/probes/r2/"], None),
    ]


def test_serving_steps_with_every_credential_end_in_the_digest(monkeypatch) -> None:
    here = module["HERE"]
    assert serve_names(monkeypatch, SITE_TOKEN="grant", SLACK_BOT_TOKEN="xoxb", SLACK_CHANNEL="C0R2USAGE", CF_PAGES_TOKEN="pages\n") == [
        ("warm-cache", ["dt-cloud", "warm-cache", "-d", "2026-10-16T0715", "-r", "r2://oa-r2-usage-index/snapshots/r2"], None),
        ("probe", ["dt-cloud", "probe", "-c", "-s", "r2", "-u", "https://r2.oa.dev", "-o", "r2://oa-r2-usage-index/probes/r2/"], None),
        ("digest", ["dt-cloud", "digest", "-T", "cw", "-C", str(here / "digest.yml"), "-i", str(here / "icons-r2"), "-r", "r2://oa-r2-usage-index/snapshots/r2"], "pages"),
    ]


def test_serving_steps_each_run_as_a_non_fatal_step(monkeypatch) -> None:
    namespace = module["serve"].__globals__
    monkeypatch.setitem(namespace, "environ", {"R2_ACCESS_KEY_ID": "index-id", "R2_SECRET_ACCESS_KEY": "index-secret", "SITE_TOKEN": "grant"})
    steps = []

    class Reporter:
        def step(self, name: str, command: list[str], env: dict[str, str], meaning: str) -> int:
            steps.append((name, meaning))
            return 0

    module["serve"](config, "2026-10-16T0715", Reporter())
    assert steps == [
        ("warm-cache", "the first viewers of this scan compute their views cold"),
        ("probe", "a page load 5xx'd or dropped (the data is fine); see the probe record"),
    ]


def test_a_failed_step_records_its_phase_failed_warns_once_and_the_scan_stands(monkeypatch, tmp_path: Path) -> None:
    from subprocess import CompletedProcess
    from dt_cloud import scan_runs

    sink = scan_runs.SqliteSink(":memory:")
    monkeypatch.setattr(scan_runs, "D1Sink", lambda db: sink)
    monkeypatch.setenv("BATCH_JOB_UID", "own-uid")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setenv("SLACK_ALERT_CHANNEL", "C0R2ALERTS")
    for name in ("DISK_TREE_NOTIFY", "DT_ALERTS"):
        monkeypatch.delenv(name, raising=False)
    posts = slack_posts(monkeypatch)
    now = clock(monkeypatch)
    monkeypatch.setitem(module["ScanReporter"].step.__globals__, "run", lambda command, env: CompletedProcess(command, 3))
    with module["ScanReporter"]("2026-10-16T0715", "20261016T071500000000Z") as reporter:
        now[0] = 110
        rc = reporter.step("probe", ["dt-cloud", "probe"], {}, "a page load 5xx'd")
    assert rc == 3
    assert sink.query("SELECT status, failed_phase, error FROM scan_runs") == [{"status": "succeeded", "failed_phase": None, "error": None}]
    assert sink.query("SELECT phase, status, note FROM scan_run_phases") == [{"phase": "probe", "status": "failed", "note": "failed (exit 3)"}]
    assert [post["text"].split("\n")[:2] for post in posts] == [["*⚠️ OA R2 scan 2026-10-16T0715*", "probe failed (exit 3): a page load 5xx'd"]]


@pytest.mark.parametrize("live", [False, True])
def test_index_gc_lists_r2_dirs_until_their_deletion_is_switched_on(monkeypatch, live: bool) -> None:
    monkeypatch.setitem(module["gc_steps"].__globals__, "environ", {
        "R2_ACCESS_KEY_ID": "index-id", "R2_SECRET_ACCESS_KEY": "index-secret",
        "DISK_TREE_R2_ENDPOINT_URL": "https://74981a43be0de7712369306c7b19133d.r2.cloudflarestorage.com",
    })
    steps = module["gc_steps"]({**config, "index_gc_files": live})
    assert [(name, command) for name, command, _, _ in steps] == ([
        ("index-gc", ["dt-cloud", "index-gc", "-s", "primary", "-m", "2d", "-F", "r2://oa-r2-usage-index"]),
        ("index-gc-meta", ["dt-cloud", "index-gc", "-s", "meta", "-m", "2d", "-F", "r2://oa-r2-usage-index"]),
    ] if live else [
        ("index-gc", ["dt-cloud", "index-gc", "-s", "primary"]),
        ("index-gc-files", ["dt-cloud", "index-gc", "-s", "primary", "-R", "-n", "-m", "2d", "-F", "r2://oa-r2-usage-index"]),
        ("index-gc-meta", ["dt-cloud", "index-gc", "-s", "meta"]),
        ("index-gc-meta-files", ["dt-cloud", "index-gc", "-s", "meta", "-R", "-n", "-m", "2d", "-F", "r2://oa-r2-usage-index"]),
    ])
    assert {(env["R2_ENDPOINT"], env["AWS_ACCESS_KEY_ID"]) for _, _, env, _ in steps} == {
        ("https://74981a43be0de7712369306c7b19133d.r2.cloudflarestorage.com", "index-id"),
    }
    assert config["index_gc_files"] is False


@pytest.mark.parametrize("killed", [False, True])
def test_supervise_finishes_a_run_killed_before_its_own_exit(monkeypatch, tmp_path: Path, killed: bool) -> None:
    """The image's entrypoint is `dt-cloud scan-run supervise -- python job/r2-scan run`: the reporter tells it which
    run it started; a run killed before `__exit__` (the OOM killer's SIGKILL) is recorded failed by the supervisor and
    alerts once, and a run that finished keeps its own record."""
    from dt_cloud import scan_runs

    sink = scan_runs.SqliteSink(":memory:")
    monkeypatch.setattr(scan_runs, "D1Sink", lambda db: sink)
    monkeypatch.setenv("BATCH_JOB_UID", "own-uid")
    monkeypatch.setenv("DT_ALERTS", "stderr")
    monkeypatch.setenv("DT_CGROUP", str(tmp_path / "no-cgroup"))
    ptr = tmp_path / "supervise.json"
    monkeypatch.setenv("DT_SUPERVISE", str(ptr))
    now = clock(monkeypatch)
    alerts = []
    monkeypatch.setattr(scan_runs.JobRun, "alert", lambda self, level, message: alerts.append((level, message)))
    reporter = module["ScanReporter"]("2026-10-16T0715", "20261016T071500000000Z").__enter__()
    now[0] = 110
    if not killed:
        reporter.__exit__(None, None, None)
    monkeypatch.delenv("DT_SUPERVISE")
    scan_runs.supervised_finish(ptr, scan_runs.Ran(137, died_of=9) if killed else scan_runs.Ran(0), 20)
    assert sink.query("SELECT status, error FROM scan_runs") == [
        {"status": "failed", "error": "killed (SIGKILL), most likely out of memory"} if killed else {"status": "succeeded", "error": None},
    ]
    assert alerts == ([("fail", "run failed (exit 137): killed (SIGKILL), most likely out of memory")] if killed else [])
    assert sorted(p.name for p in (tmp_path / "dt-scan-run").iterdir()) == []
