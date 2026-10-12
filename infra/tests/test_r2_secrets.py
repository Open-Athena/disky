from pathlib import Path
from runpy import run_path
from stat import S_IMODE

import pytest


module = run_path(str(Path(__file__).resolve().parents[1] / "cf/sync-secrets"))


def test_pages_gets_only_reader_and_session_secrets() -> None:
    outputs = {
        "index_s3_access_key_id": "reader-id",
        "index_s3_secret_access_key": "reader-secret",
        "r2_s3_access_key_id": "publisher-id",
        "r2_s3_secret_access_key": "publisher-secret",
        "source_s3_access_key_id": "source-id",
        "source_s3_secret_access_key": "source-secret",
    }
    assert module["pages_secrets"](outputs, "account", "session") == {
        "SESSION_SECRET": "session",
        "STORE_ENDPOINT": "https://account.r2.cloudflarestorage.com",
        "STORE_ACCESS_KEY_ID": "reader-id",
        "STORE_SECRET_ACCESS_KEY": "reader-secret",
    }


def test_session_secret_is_private_and_stable(tmp_path: Path) -> None:
    path = tmp_path / "session-secret"
    value = module["private_session"](path)
    assert len(value) == 64
    assert len(bytes.fromhex(value)) == 32
    assert path.read_text() == value + "\n"
    assert S_IMODE(path.stat().st_mode) == 0o600
    assert module["private_session"](path) == value


def test_job_gets_distinct_source_publisher_and_d1_credentials() -> None:
    assert module["job_secrets"]({
        "source_s3_access_key_id": "source-id", "source_s3_secret_access_key": "source-secret",
        "r2_s3_access_key_id": "publisher-id", "r2_s3_secret_access_key": "publisher-secret",
        "index_s3_access_key_id": "reader-id", "index_s3_secret_access_key": "reader-secret",
        "d1_publish_token": "d1-token",
    }) == {
        "SOURCE_ACCESS_KEY_ID": "source-id", "SOURCE_SECRET_ACCESS_KEY": "source-secret",
        "R2_ACCESS_KEY_ID": "publisher-id", "R2_SECRET_ACCESS_KEY": "publisher-secret",
        "CLOUDFLARE_API_TOKEN": "d1-token",
    }


def test_invalid_saved_session_is_not_rotated(tmp_path: Path) -> None:
    path = tmp_path / "session-secret"
    path.write_text("invalid\n")
    with pytest.raises(ValueError, match="^Invalid saved session secret$"):
        module["private_session"](path)
    assert path.read_text() == "invalid\n"


@pytest.fixture
def oauth_client() -> dict:
    origins = ["https://r2.oa.dev", "https://dev.r2.oa.dev", "https://r2.oa-r2-usage.pages.dev", "http://localhost:3273", "http://127.0.0.1:3273"]
    return {"web": {
        "project_id": "oa-auth-509611", "client_id": "r2.apps.googleusercontent.com", "client_secret": "new-secret",
        "javascript_origins": origins, "redirect_uris": [origin + "/auth/google/callback" for origin in origins],
    }}


def test_oauth_secrets_are_validated_for_only_this_deployment(oauth_client: dict) -> None:
    assert module["oauth_secrets"](oauth_client) == {"GOOGLE_CLIENT_ID": "r2.apps.googleusercontent.com", "GOOGLE_CLIENT_SECRET": "new-secret"}


def test_other_apps_redirect_is_rejected(oauth_client: dict) -> None:
    oauth_client["web"]["redirect_uris"].append("https://cw-s3.oa.dev/auth/google/callback")
    with pytest.raises(ValueError, match="^OAuth origins and callbacks must match only this deployment$"):
        module["oauth_secrets"](oauth_client)


def test_wrong_auth_project_is_rejected(oauth_client: dict) -> None:
    oauth_client["web"]["project_id"] = "oa-internal-450019"
    with pytest.raises(ValueError, match="^OAuth client must use the OA auth project$"):
        module["oauth_secrets"](oauth_client)
