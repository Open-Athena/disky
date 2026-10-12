from datetime import datetime, timezone
from json import loads
from pathlib import Path
from runpy import run_path

import pytest


module = run_path(str(Path(__file__).resolve().parents[1] / "r2-scan"))
config = module["profile"]()
image = "us-west1-docker.pkg.dev/oa-internal-450019/oa-r2-usage/scan@sha256:" + "a" * 64


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
