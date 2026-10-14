"""Provision an expiring monitor-only grant and finite-calendar HTTP cron.

Raw credentials stay in memory, Secret Manager and the Scheduler target header,
never argv, logs or tracked config. The calendar cron has no year field; the
endpoint's absolute end instant also refuses all future monitoring.
"""
from datetime import datetime
from json import dumps, loads
from pathlib import Path
from subprocess import run
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from click import Choice, argument, command, echo, option

from .gcp import session
from .job_token import provision


def profile(path: str) -> dict:
    config = loads(Path(path).read_text())
    expected = {"project", "region", "name", "secret", "url", "scope", "until", "schedule"}
    if set(config) != expected:
        raise ValueError("session-monitor: invalid profile fields")
    if config["scope"] != "gcs:session-monitor:read" or config["url"] != "https://gcs.oa.dev/api/session-monitor" or config["until"] != "2026-10-21T00:00:00Z" or config["schedule"] != "*/5 * 13-20 10 *":
        raise ValueError("session-monitor: profile exceeds the approved endpoint/calendar/expiry")
    return config


def target(config: dict, token: str) -> dict:
    return {
        "name": f"projects/{config['project']}/locations/{config['region']}/jobs/{config['name']}",
        "description": "Temporary GCS aggregate session-log checker through 2026-10-20 UTC; 3 notification attempts/day maximum",
        "schedule": config["schedule"],
        "timeZone": "Etc/UTC",
        "attemptDeadline": "30s",
        "retryConfig": {"retryCount": 0, "maxRetryDuration": "0s"},
        "httpTarget": {
            "uri": config["url"], "httpMethod": "POST",
            "headers": {"Authorization": f"Bearer {token}"},
        },
    }


def secret_token(config: dict) -> str:
    return run(["gcloud", "secrets", "versions", "access", "latest", "--secret", config["secret"], "--project", config["project"]], check=True, capture_output=True, text=True).stdout.strip()


def verify(
    config: dict,
    token: str,
    dry: bool,
) -> dict:
    req = Request(config["url"] + ("?dry=1" if dry else ""), method="POST", headers={"Authorization": f"Bearer {token}", "User-Agent": "dt-cloud/session-monitor"})
    try:
        with urlopen(req, timeout=30) as response:
            result = loads(response.read(65_536))
    except HTTPError as exc:
        raise ValueError(f"session-monitor: check HTTP {exc.code}") from None
    if dry and result.get("status") != "dry-run":
        raise ValueError("session-monitor: dry-run did not confirm active monitoring")
    return result


def audit(config: dict, token: str) -> dict[str, int]:
    origin = config["url"].removesuffix("/api/session-monitor")
    results = {}
    for path in ("/api/me", "/api/session-log/sessions", "/api/db/admin_edits"):
        req = Request(origin + path, headers={"Authorization": f"Bearer {token}", "User-Agent": "dt-cloud/session-monitor-scope-audit"})
        try:
            with urlopen(req, timeout=30) as response:
                status = response.status
        except HTTPError as exc:
            status = exc.code
        if status != 403:
            raise ValueError(f"session-monitor: scope audit {path} HTTP {status}, expected 403")
        results[path] = status
    return results


def bootstrap(config: dict) -> str:
    client = session()
    secret_url = f"https://secretmanager.googleapis.com/v1/projects/{config['project']}/secrets/{config['secret']}"
    existing = client.get(secret_url, timeout=30)
    if existing.status_code == 404:
        created = client.post(secret_url.rsplit('/', 1)[0], params={"secretId": config["secret"]}, json={"replication": {"automatic": {}}, "labels": {"app": "disky", "deployment": "gcs", "component": "session-monitor"}}, timeout=30)
        if not created.ok:
            raise ValueError(f"session-monitor: secret create HTTP {created.status_code}")
    elif not existing.ok:
        raise ValueError(f"session-monitor: secret lookup HTTP {existing.status_code}")
    grant_id = provision(config["project"], config["secret"], "GCS session monitor", config["scope"],
                         expires_at=int(datetime.fromisoformat(config["until"].replace('Z', '+00:00')).timestamp()),
                         note="Temporary aggregate session monitor only; no raw logs, viewer, assignment or deletion privileges")
    return grant_id


def install(config: dict) -> dict:
    client = session()
    grant_id = bootstrap(config)
    token = secret_token(config)
    checked = verify(config, token, True)
    body = target(config, token)
    url = f"https://cloudscheduler.googleapis.com/v1/{body['name']}"
    existing = client.get(url, timeout=30)
    if existing.ok:
        current = existing.json()
        if current.get("httpTarget", {}).get("uri") != config["url"]:
            raise ValueError("session-monitor: existing scheduler target differs; refusing to replace it")
        result = client.patch(url, params={"updateMask": "description,schedule,timeZone,attemptDeadline,retryConfig,httpTarget"}, json=body, timeout=30)
    elif existing.status_code == 404:
        result = client.post(url.rsplit('/', 1)[0], json=body, timeout=30)
    else:
        raise ValueError(f"session-monitor: scheduler lookup HTTP {existing.status_code}")
    if not result.ok:
        raise ValueError(f"session-monitor: scheduler write HTTP {result.status_code}")
    # The response contains the target credential: return only a strict safe projection.
    return {"grant": grant_id, "scheduler": body["name"], "schedule": body["schedule"], "until": config["until"], "dry_run": checked}


@command("session-monitor")
@option("-a", "--action", type=Choice(["provision", "install", "check", "dry-run", "audit", "status", "pause"]), default="dry-run", help="Provision expiring token, install cron, or inspect/check/pause the monitor")
@option("-d", "--dev", is_flag=True, help="Dry-run only: read the dev endpoint (no state writes or Slack)")
@argument("config_path")
def cli(
    action: str,
    dev: bool,
    config_path: str,
) -> None:
    """Time-boxed, failure-only session monitoring (job/session-monitor.json)."""
    config = profile(config_path)
    if dev:
        if action != "dry-run":
            raise ValueError("session-monitor: --dev is only allowed with dry-run")
        config = {**config, "url": config["url"].replace("https://gcs.oa.dev", "https://dev.gcs.oa.dev")}
    if action == "provision":
        result = {"grant": bootstrap(config), "until": config["until"]}
    elif action == "install":
        result = install(config)
    elif action in ("check", "dry-run"):
        result = verify(config, secret_token(config), action == "dry-run")
    elif action == "audit":
        result = audit(config, secret_token(config))
    else:
        url = f"https://cloudscheduler.googleapis.com/v1/projects/{config['project']}/locations/{config['region']}/jobs/{config['name']}"
        response = session().post(url + ":pause", json={}, timeout=30) if action == "pause" else session().get(url, timeout=30)
        if not response.ok:
            raise ValueError(f"session-monitor: scheduler {action} HTTP {response.status_code}")
        row = response.json()
        result = {key: row.get(key) for key in ("name", "state", "schedule", "scheduleTime", "lastAttemptTime", "status")}
    echo(dumps(result, indent=2, sort_keys=True))
