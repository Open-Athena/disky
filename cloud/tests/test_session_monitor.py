from copy import deepcopy
from json import dumps
from pathlib import Path
from urllib.error import HTTPError

import pytest

from dt_cloud import session_monitor as M


CONFIG_PATH = Path(__file__).resolve().parents[2] / "job/session-monitor.json"


def test_profile_and_target_are_bounded_and_no_retry():
    config = M.profile(str(CONFIG_PATH))
    assert M.target(config, "test-token") == {
        "name": "projects/oa-internal-450019/locations/us-central1/jobs/gcs-session-monitor",
        "description": "Temporary GCS aggregate session-log checker through 2026-10-20 UTC; 3 notification attempts/day maximum",
        "schedule": "*/5 * 13-20 10 *",
        "timeZone": "Etc/UTC",
        "attemptDeadline": "30s",
        "retryConfig": {"retryCount": 0, "maxRetryDuration": "0s"},
        "httpTarget": {"uri": "https://gcs.oa.dev/api/session-monitor", "httpMethod": "POST", "headers": {"Authorization": "Bearer test-token"}},
    }


@pytest.mark.parametrize("key,value", [("scope", "admin"), ("url", "https://other.test/api/session-monitor"), ("until", "2027-01-01T00:00:00Z"), ("schedule", "* * * * *")])
def test_profile_refuses_scope_or_time_expansion(tmp_path, key, value):
    config = M.profile(str(CONFIG_PATH))
    config[key] = value
    path = tmp_path / "profile.json"
    path.write_text(dumps(config))
    with pytest.raises(ValueError) as exc:
        M.profile(str(path))
    assert str(exc.value) == "session-monitor: profile exceeds the approved endpoint/calendar/expiry"


def test_install_verifies_before_schedule_and_redacts_result(monkeypatch):
    config = M.profile(str(CONFIG_PATH))
    calls = []

    class Response:
        status_code = 200
        ok = True

        def json(self):
            return {"httpTarget": {"uri": config["url"], "headers": {"Authorization": "Bearer test-token"}}}

    class Client:
        def get(self, url, **kwargs):
            calls.append(("get", url, kwargs))
            return Response()

        def patch(self, url, **kwargs):
            calls.append(("patch", url, deepcopy(kwargs)))
            return Response()

    monkeypatch.setattr(M, "session", lambda: Client())
    monkeypatch.setattr(M, "provision", lambda *args, **kwargs: calls.append(("provision", args, kwargs)) or "grant-id")
    monkeypatch.setattr(M, "secret_token", lambda cfg: "test-token")
    monkeypatch.setattr(M, "verify", lambda cfg, tok, dry: calls.append(("verify", cfg, tok, dry)) or {"status": "dry-run"})
    assert M.install(config) == {
        "grant": "grant-id",
        "scheduler": "projects/oa-internal-450019/locations/us-central1/jobs/gcs-session-monitor",
        "schedule": "*/5 * 13-20 10 *", "until": "2026-10-21T00:00:00Z", "dry_run": {"status": "dry-run"},
    }
    url = "https://cloudscheduler.googleapis.com/v1/projects/oa-internal-450019/locations/us-central1/jobs/gcs-session-monitor"
    assert calls == [
        ("get", "https://secretmanager.googleapis.com/v1/projects/oa-internal-450019/secrets/gcs-session-monitor-token", {"timeout": 30}),
        ("provision", (config["project"], config["secret"], "GCS session monitor", config["scope"]), {"expires_at": 1792540800, "note": "Temporary aggregate session monitor only; no raw logs, viewer, assignment or deletion privileges"}),
        ("verify", config, "test-token", True),
        ("get", url, {"timeout": 30}),
        ("patch", url, {"params": {"updateMask": "description,schedule,timeZone,attemptDeadline,retryConfig,httpTarget"}, "json": M.target(config, "test-token"), "timeout": 30}),
    ]


def test_failed_preflight_never_schedules(monkeypatch):
    config = M.profile(str(CONFIG_PATH))
    calls = []

    class Client:
        def get(self, url, **kwargs):
            calls.append(url)
            return type("Response", (), {"status_code": 200, "ok": True})()

    monkeypatch.setattr(M, "session", lambda: Client())
    monkeypatch.setattr(M, "provision", lambda *args, **kwargs: "grant-id")
    monkeypatch.setattr(M, "secret_token", lambda cfg: "test-token")

    def verify(cfg, token, dry):
        raise ValueError("preflight failed")

    monkeypatch.setattr(M, "verify", verify)
    with pytest.raises(ValueError) as exc:
        M.install(config)
    assert str(exc.value) == "preflight failed"
    assert calls == ["https://secretmanager.googleapis.com/v1/projects/oa-internal-450019/secrets/gcs-session-monitor-token"]


def test_live_scope_audit_checks_only_fixed_read_endpoints(monkeypatch):
    config = M.profile(str(CONFIG_PATH))
    calls = []

    def forbidden(request, timeout):
        calls.append((request.full_url, dict(request.header_items()), timeout))
        raise HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(M, "urlopen", forbidden)
    assert M.audit(config, "test-token") == {"/api/me": 403, "/api/session-log/sessions": 403, "/api/db/admin_edits": 403}
    assert calls == [(f"https://gcs.oa.dev{path}", {"Authorization": "Bearer test-token", "User-agent": "dt-cloud/session-monitor-scope-audit"}, 30) for path in ("/api/me", "/api/session-log/sessions", "/api/db/admin_edits")]
