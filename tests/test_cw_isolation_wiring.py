import json
import os
from pathlib import Path
from subprocess import run
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "oa-internal-450019"
IMAGE = f"us-central1-docker.pkg.dev/{PROJECT}/cw-s3-usage/scan:cw"
DATA = "oa-cw-s3-usage-data"
R2_SECRETS = {
    "R2_ENDPOINT": "cw-s3-r2-endpoint",
    "R2_ACCESS_KEY_ID": "cw-s3-isolated-r2-access-key-id",
    "R2_SECRET_ACCESS_KEY": "cw-s3-isolated-r2-secret-access-key",
}
CF = {"CLOUDFLARE_API_TOKEN": "cw-s3-cf-pages-token"}


@pytest.mark.parametrize("script, args, expected_secrets", [
    ("cw-batch-submit.sh", [], {**CF, **R2_SECRETS, "AWS_ACCESS_KEY_ID": "cw-s3-access-key-id", "AWS_SECRET_ACCESS_KEY": "cw-s3-secret-access-key", "SLACK_BOT_TOKEN": "cw-s3-slack-bot-token", "SITE_TOKEN": "cw-s3-job-grant"}),
    ("cw-meta-submit.sh", [], {**CF, **R2_SECRETS, "AWS_ACCESS_KEY_ID": "cw-s3-isolated-r2-access-key-id", "AWS_SECRET_ACCESS_KEY": "cw-s3-isolated-r2-secret-access-key", "SLACK_BOT_TOKEN": "cw-s3-slack-bot-token", "SITE_TOKEN": "cw-s3-job-grant"}),
    ("cw-reindex-submit.sh", [], CF),
    ("cw-overtime-submit.sh", [], {**CF, **R2_SECRETS}),
    ("cw-publish-submit.sh", ["2026-10-10T1201"], {**CF, **R2_SECRETS}),
    ("cw-recompress-submit.sh", ["2026-10-10T1201"], {}),
])
def test_submitters_use_only_reviewed_isolated_targets(
    script: str,
    args: list[str],
    expected_secrets: dict,
    tmp_path: Path,
) -> None:
    result = run(["bash", str(ROOT / "job" / script), *args], capture_output=True, text=True, check=True,
                 cwd=ROOT, env={"PATH": os.environ["PATH"], "DRY": "1", "PIN": "1", "TMPDIR": str(tmp_path)})
    spec = json.loads(result.stdout)
    task = spec["taskGroups"][0]["taskSpec"]
    assert {"image": task["runnables"][0]["container"]["imageUri"],
            "data": task["environment"]["variables"]["DATA_BUCKET"],
            "sa": spec["allocationPolicy"]["serviceAccount"],
            "secrets": task["environment"].get("secretVariables", {})} == {
        "image": IMAGE, "data": DATA,
        "sa": {"email": f"cw-s3-job@{PROJECT}.iam.gserviceaccount.com"},
        "secrets": {var: f"projects/{PROJECT}/secrets/{sid}/versions/latest" for var, sid in expected_secrets.items()},
    }
    if script in ("cw-batch-submit.sh", "cw-meta-submit.sh", "cw-reindex-submit.sh", "cw-overtime-submit.sh"):
        assert task["volumes"][0] == {"gcs": {"remotePath": DATA}, "mountPath": f"/mnt/disks/gcs/{DATA}", "mountOptions": ["--implicit-dirs"]}
    if script == "cw-meta-submit.sh":
        assert task["environment"]["variables"]["META_ROOTS"] == f"gcs://{DATA} r2://oa-cw-s3-usage-index"
    if script == "cw-batch-submit.sh":
        assert task["environment"]["variables"]["STATIC_NAMES_IMAGE"] == IMAGE


def test_static_profile_uses_isolated_data_scratch_image_and_publisher() -> None:
    profile = json.loads((ROOT / "job/static-names.json").read_text())
    assert {name: profile[name] for name in ("bucket", "scratch", "image", "r2_secrets", "gen")} == {
        "bucket": DATA, "scratch": "oa-cw-s3-usage-scratch", "image": IMAGE, "gen": "2026-10-10cw",
        "r2_secrets": {"endpoint": "cw-s3-r2-endpoint", "key_id": "cw-s3-isolated-r2-access-key-id", "secret": "cw-s3-isolated-r2-secret-access-key"},
    }


@pytest.mark.parametrize("environment", ["production", "preview"])
def test_both_site_environments_dispatch_and_describe_cw_only(environment: str) -> None:
    config = tomllib.loads((ROOT / "site/wrangler.toml").read_text())
    variables = config["vars"] if environment == "production" else config["env"]["preview"]["vars"]
    assert {name: variables[name] for name in ("DATA_BUCKET", "SWEEP_IMAGE", "STORE_BUCKET")} == {
        "DATA_BUCKET": DATA, "SWEEP_IMAGE": IMAGE, "STORE_BUCKET": "oa-cw-s3-usage-index",
    }
    meta = json.loads(variables["STORES_JSON"])["meta"]["vars"]
    assert {name: meta[name] for name in ("STORE_BUCKETS", "STORE_BUCKET", "ROOT_TITLE")} == {
        "STORE_BUCKETS": DATA + ",oa-cw-s3-usage-index", "STORE_BUCKET": "oa-cw-s3-usage-index",
        "ROOT_TITLE": f"Data behind this site: scans, indexes and listings in {DATA} (GCS) and oa-cw-s3-usage-index (R2)",
    }
