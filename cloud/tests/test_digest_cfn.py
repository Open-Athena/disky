"""The digest's Workers converge, the job's side (specs/slack-in-workers.md): the converge config a site reads
(`digest.converge_config`, `dt-cloud digest-config`) and the driver that steps the site's job (`dt-cloud digest-cfn`)."""
from __future__ import annotations

import json
from dataclasses import replace

from click.testing import CliRunner

from dt_cloud import cli
from dt_cloud import digest as E
from dt_cloud import site

from digest_examples import CW_EXAMPLE


def test_converge_config():
    cfg = replace(CW_EXAMPLE, op_icon="mark.png", variant="weekly", preview="gs://d/digest/preview/{channel}/{variant}")
    c = E.converge_config(E.template(cfg), "gs://d/snapshots/cw")
    assert {k: v for k, v in c.items() if k != "cfg"} == {
        "version": 1, "template": "quota", "params": "",
        "reply": {"quota_marks": "warning", "total_headline": False, "reply_hour": 12, "provisional": False, "alert_pct": None,
                  "op_sender": {"icon_url": "https://icons.example.org/mark.png"}, "avatar_rev": c["reply"]["avatar_rev"]},
        "root": "gs://d/snapshots/cw", "state": "digest/cw/{channel}/{variant}", "preview": "gs://d/digest/preview/{channel}/{variant}",
        "variant": "weekly", "variants": ["sender", "body", "weekly"], "edited_variants": ["body", "weekly"], "track_scan": True,
        "pin": True, "split_reply": False, "discord": False, "title": "S3 usage", "op_sender": {"icon_url": "https://icons.example.org/mark.png"},
    }
    assert c["cfg"] == E.cfg_json(cfg)


def _job(state: str, steps: int, **kw) -> dict:
    return {"id": "digest-converge-20261201t071500000z-0a1b2c3d", "state": state, "steps": steps, "calls": 3, "shadow": False,
            "error": None, "retry_at": None, "report": None, **kw}


def test_digest_cfn_steps_until_done(monkeypatch):
    answers = [_job("running", 1, error="step budget: 40 Slack calls"), _job("done", 2, report={"month": "2026-12"})]
    calls: list[tuple] = []
    monkeypatch.setenv("SITE_URL", "https://r2.example.org")
    monkeypatch.setenv("SITE_TOKEN", "t")
    monkeypatch.setattr(site, "post_json", lambda url, token, path, body, **kw: calls.append((url, path, body)) or json.dumps({"job": answers.pop(0)}).encode())
    r = CliRunner().invoke(cli.main, ["digest-cfn", "-m", "2026-12"])
    assert (r.exit_code, calls) == (0, [("https://r2.example.org", "/api/digest/converge", {"month": "2026-12"})] * 2)


def test_digest_cfn_prints_a_shadows_audit_and_fails_a_failed_job(monkeypatch):
    audit = [{"what": "op", "ts": "1.1", "same": True}, {"what": "wk-2026-11-30", "ts": "1.2", "same": False, "live": "a", "want": "b"}]
    answers = [_job("done", 1, shadow=True, report={"month": "2026-12", "variant": "weekly", "audit": audit, "writes": [{}]}),
               _job("failed", 1, error="a switch, which a Worker doesn't run yet")]
    monkeypatch.setenv("SITE_TOKEN", "t")
    monkeypatch.setattr(site, "post_json", lambda url, token, path, body, **kw: json.dumps({"job": answers.pop(0)}).encode())
    r = CliRunner().invoke(cli.main, ["digest-cfn", "-u", "https://dev.r2.example.org"])
    assert (r.exit_code, r.stdout.split("\n")) == (0, [
        "shadow 2026-12 (weekly): 1/2 messages as rendered; 1 writes it would make",
        "  differs: wk-2026-11-30 1.2",
        "    live: 'a'",
        "    want: 'b'",
        "",
    ])
    r = CliRunner().invoke(cli.main, ["digest-cfn", "-u", "https://dev.r2.example.org"])
    assert (r.exit_code, r.output.strip().split("\n")[-1]) == (1, "digest-cfn: digest-converge-20261201t071500000z-0a1b2c3d failed: a switch, which a Worker doesn't run yet")
