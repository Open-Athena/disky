"""`dt-cloud scan head` (specs/pipelines-everywhere.md §2.3): one scan as a Nextflow run of `job/nf/scan.nf`.

It stamps the scan's identity once (`SNAP_ID`, `GEN`, `DT_JOB_START`), starts its scan-run record, derives the
Nextflow session from the scan (uuid5 of `disky/<deployment>/scan/<scan>`), writes the session's `.nextflow/history`
line (Nextflow finds a session only there; its cache is in the work store, `NXF_CLOUDCACHE_PATH`), writes the params
and the executor config from the profile, runs Nextflow with `-resume <session>`, and finishes the record with
Nextflow's exit status (the first failed task named) and the labelled cost of its tasks. Re-running the head for a
failed scan (`SNAP_ID=<scan> GEN=<its gen>`), anywhere, re-runs only the tasks that didn't finish.

A TERM / INT (a cancelled or preempted head job) stops Nextflow's process group (the JVM is the launcher script's
child), waits `NF_SWEEP_DELAY` (30 s: a submit in flight lands after Nextflow exits), cancels whatever Batch job still
carries the session's `nf-session` label, and finishes the record.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

from .scan_profile import Disk, ScanProfile
from .scan_steps import DATA_MOUNT_ENV, PROFILE_JSON_ENV, STEPS


def err(*a: object) -> None:
    print(*a, file=sys.stderr)

NF_GOOGLE = "nf-google@1.29.1"
NF_CLOUDCACHE = "nf-cloudcache@0.6.2"
#: Where a google-batch task sees its work dir's bucket (and so the data store): Batch mounts it at `/mnt/disks/<b>`.
BATCH_MOUNT = "/mnt/disks"


def identity(env: Mapping[str, str], now: dt.datetime | None = None) -> tuple[str, str, str]:
    """(SNAP_ID, GEN, DT_JOB_START): each from `env` when set (a resumed scan keeps its own), else stamped now."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return (env.get("SNAP_ID") or now.strftime("%Y-%m-%dT%H%M"), env.get("GEN") or now.strftime("%Y%m%dT%H%M%SZ"),
            env.get("DT_JOB_START") or str(int(now.timestamp())))


def session_id(deployment: str, scan: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"disky/{deployment}/scan/{scan}"))


def history_line(now: dt.datetime, scan: str, session: str) -> str:
    """The one `.nextflow/history` line `-resume <session>` needs."""
    run_name = "scan_" + "".join(c if c.isalnum() else "_" for c in scan)
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')}\t-\t{run_name}\tOK\t-\t{session}\tnextflow run scan.nf\n"


def labels(profile: ScanProfile, env: Mapping[str, str]) -> dict[str, str]:
    """The tasks' Batch labels: the profile's, the stack's (`$DISKY_LABELS`, `k=v,…`), `component: scan`; scan.nf
    adds `nf-session`."""
    out = dict(profile.labels)
    for i in (env.get("DISKY_LABELS") or "").split(","):
        if i.strip():
            k, _, v = i.strip().partition("=")
            out[k] = v
    out["component"] = "scan"
    return out


def _disk(d: Disk | None) -> dict | None:
    return None if d is None else {"request": d.request, "type": d.type}


def params(profile: ScanProfile, profile_json: str, scan: str, gen: str, job_start: str, run_id: str, session: str,
           env: Mapping[str, str], *, reproc: bool = False, fresh: bool = False) -> dict:
    """`scan.nf`'s params file."""
    enabled = profile.enabled()
    step_env = dict(profile.env)
    step_env.update({k: env[k] for k in profile.passthrough if k in env})
    if reproc:
        step_env["REPROC"] = "1"
    if profile.executor.kind == "google-batch":
        step_env[DATA_MOUNT_ENV] = BATCH_MOUNT
    lst = profile.machines.get("list")
    return {
        "scan": scan, "gen": gen, "job_start": job_start, "run_id": run_id, "session": session,
        "sources": [{"uri": s.uri, "machine": s.machine, "disk": _disk(s.disk)} for s in profile.sources],
        "list_default": {"machine": lst.type if lst else None, "disk": _disk(lst.disk) if lst else None},
        "steps": list(enabled),
        "pre": [n for n in enabled if STEPS[n].stage == "pre" and n != "bucket-check"],
        "pre_label": "pre" if "pre" in profile.machines else "small",
        "interval_label": "interval" if "interval" in profile.machines else "big",
        "post": [n for n in enabled if STEPS[n].stage == "post" and n != "over-time"],
        "over_time": "over-time" in enabled,
        "over_time_label": "over_time" if "over_time" in profile.machines else "post",
        "fold": list(profile.fold),
        "skip_list": reproc,
        "secrets": [f"{k}={v}" for k, v in profile.secrets.items()],
        "secrets_project": profile.executor.project or "",
        "env": step_env,
        "profile_json": profile_json,
        "labels": labels(profile, env),
        "cache": not fresh,
        "queue_size": profile.queue_size,
        "work_bucket": urlparse(profile.work).netloc if profile.executor.kind == "google-batch" else "",
    }


def _q(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _disk_cfg(d: Disk) -> str:
    return f"{d.request}.GB" if d.type is None else f"[request: {d.request}.GB, type: {_q(d.type)}]"


def executor_config(profile: ScanProfile) -> str:
    """The deployment's executor block, generated (never hand-written per deployment): work dir, plugins, the
    executor's own scope, the image, and one `withLabel` per machine (`list` excepted: scan.nf sets each list task's
    machine from its source, falling back to `machines.list`, since a config selector would override it)."""
    ex = profile.executor
    lines = [f"// Generated by `dt-cloud scan head` from the {profile.deployment} profile: not edited by hand.",
             f"workDir = {_q(profile.work.rstrip('/') + '/work')}"]
    if ex.kind == "google-batch":
        lines += ["plugins {", f"    id {_q(NF_GOOGLE)}", f"    id {_q(NF_CLOUDCACHE)}", "}", "google {",
                  f"    project = {_q(ex.project)}", f"    location = {_q(ex.region)}"]
        if ex.service_account:
            lines.append(f"    batch.serviceAccountEmail = {_q(ex.service_account)}")
        if ex.boot_disk:
            lines.append(f"    batch.bootDiskSize = {ex.boot_disk}.GB")
        if ex.spot is not None:
            lines.append(f"    batch.spot = {'true' if ex.spot else 'false'}")
        lines.append("}")
    elif ex.kind == "awsbatch":
        lines += ["aws {", f"    region = {_q(ex.region)}"]
        if ex.cli_path:
            lines.append(f"    batch.cliPath = {_q(ex.cli_path)}")
        lines.append("}")
    lines.append("process {")
    lines.append(f"    executor = {_q(ex.kind)}")
    if ex.kind == "awsbatch":
        lines += [f"    queue = {_q(ex.queue)}", "    containerOptions = '--entrypoint=/usr/bin/env'"]
    if ex.image:
        lines.append(f"    container = {_q(ex.image)}")
    for label, m in sorted(profile.machines.items()):
        if label == "list":
            continue
        ds = []
        if m.type and ex.kind == "google-batch":
            ds.append(f"machineType = {_q(m.type)}")
        if m.disk and ex.kind == "google-batch":
            ds.append(f"disk = {_disk_cfg(m.disk)}")
        if m.cpus:
            ds.append(f"cpus = {m.cpus}")
        if m.memory:
            ds.append(f"memory = {_q(m.memory)}")
        if m.time:
            ds.append(f"time = {_q(m.time)}")
        if ds:
            lines.append(f"    withLabel: {label} {{ {'; '.join(ds)} }}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def first_failed(trace: Path) -> str | None:
    """`<task> (exit N)` for the trace's first FAILED task."""
    try:
        with trace.open() as f:
            for row in csv.DictReader(f, delimiter="\t"):
                if row.get("status") == "FAILED":
                    return f"{row['name']} (exit {row.get('exit', '-')})"
    except OSError:
        return None
    return None


def _record(cmd, args: list[str]) -> None:
    """A `scan-run` subcommand, in-process (they never fail the job)."""
    try:
        cmd.main(args, prog_name=f"dt-cloud scan-run {cmd.name}", standalone_mode=False)
    except SystemExit:
        pass


#: Nextflow's run reports, written in the launch dir and uploaded beside the run (`runs_prefix`): the DAG (Mermaid; the
#: site draws it with each node's phase status), the timeline, the resource report and the trace.
REPORTS = ("dag.mmd", "timeline.html", "report.html", "trace.tsv")


def runs_prefix(scan: str) -> str:
    """Where a scan's run reports go, under the profile's work store: `<work>/runs/<scan>/` (e.g. a deployment's
    `scan-runs.json` output `nf-dag` = `<work>/runs/{scan}/dag.mmd`)."""
    return f"runs/{scan}"


def publish_reports(profile: ScanProfile, scan: str, launch: Path) -> list[str]:
    """Upload the run's reports that exist to `<work>/runs/<scan>/` (a re-run replaces them); returns the keys. Never fails
    the run: a missing report (Nextflow stopped early) or an upload error is logged."""
    from .blobstore import Store

    store, keys = Store(profile.work), []
    for name in REPORTS:
        src = launch / name
        if not src.is_file():
            continue
        key = f"{runs_prefix(scan)}/{name}"
        try:
            store.put(src, key)
            keys.append(key)
        except Exception as e:  # noqa: BLE001 — reports are a view of the run, not its output
            err(f"scan head: upload {key} failed: {type(e).__name__}: {e}")
    if keys:
        err(f"scan head: reports → {store.uri(runs_prefix(scan))}/ ({', '.join(k.rsplit('/', 1)[1] for k in keys)})")
    return keys


def finish_args(rc: int, error: str | None, cost: list[str], dag: Path) -> list[str]:
    """`scan-run finish`'s arguments: the exit status, the error, the cost labels, and the run's DAG (`-D`, stored with
    the run for the site's pipeline view) when Nextflow wrote one."""
    return ["-x", str(rc), *(["-e", error] if error else []), *cost, *(["-D", str(dag)] if dag.is_file() else [])]


def head(profile_path: Path, nf_dir: Path, launch: Path | None, *, reproc: bool = False, fresh: bool = False,
         dry_run: bool = False, env: dict[str, str] | None = None) -> int:
    from . import scan_profile
    from .scan_runs import PROFILE_ENV, finish_cmd, start_cmd

    env = os.environ if env is None else env
    profile = scan_profile.load(profile_path)
    profile_json = scan_profile.to_json(profile_path)
    now = dt.datetime.now(dt.timezone.utc)
    scan, gen, job_start = identity(env, now)
    run_id = env.get("DT_RUN_ID") or env.get("BATCH_JOB_UID") or ""
    env.update(SNAP_ID=scan, SCAN_ID=scan, GEN=gen, DT_JOB_START=job_start, DT_RUN_ID=run_id)
    for k in (PROFILE_ENV, "D1_DB_ID", "D1_DB_NAME", "SLACK_ALERT_CHANNEL", "DT_ALERTS"):
        if k in profile.env:
            env.setdefault(k, profile.env[k])
    if reproc:
        env.update(REPROC="1", DT_RECORD="off")
    session = session_id(profile.deployment, scan)
    cloud = profile.executor.kind != "local"
    if launch is None:
        launch = Path(profile.work) if not cloud else Path(env.get("TMPDIR") or "/tmp") / f"nf-{scan}"
    launch = launch.absolute()
    (launch / ".nextflow").mkdir(parents=True, exist_ok=True)
    (launch / ".nextflow" / "history").write_text(history_line(now, scan, session))
    (launch / "params.json").write_text(json.dumps(
        params(profile, profile_json, scan, gen, job_start, run_id, session, env, reproc=reproc, fresh=fresh), indent=1) + "\n")
    (launch / "executor.config").write_text(executor_config(profile))
    trace = launch / "trace.tsv"
    for name in REPORTS:
        (launch / name).unlink(missing_ok=True)
    argv = ["nextflow", "run", str(nf_dir / "scan.nf"), "-c", str(nf_dir / "base.config"), "-c", str(launch / "executor.config"),
            "-params-file", str(launch / "params.json"), "-resume", session, "-with-trace", str(trace),
            "-with-dag", str(launch / "dag.mmd"), "-with-timeline", str(launch / "timeline.html"), "-with-report", str(launch / "report.html")]
    nf_env = {**env, "NXF_ANSI_LOG": "false"}
    if cloud:
        nf_env["NXF_CLOUDCACHE_PATH"] = profile.work.rstrip("/") + "/cache"
    err(f"scan head: {profile.deployment} scan {scan} gen {gen} session {session} in {launch}")
    err("+ " + " ".join(argv))
    if dry_run:
        return 0
    recording = bool(env.get(PROFILE_ENV))
    cost = ["-l", f"nf-session={session}"] if profile.executor.kind == "google-batch" else []
    if recording:
        _record(start_cmd, ["-B", "-g", gen, scan])

    proc = subprocess.Popen(argv, cwd=launch, env=nf_env, start_new_session=True)
    stopped: list[int] = []

    def stop(sig: int, frame) -> None:  # noqa: ANN001
        if not stopped:
            stopped.append(128 + sig)
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    prev = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        rc = proc.wait()
    finally:
        for s, h in prev.items():
            signal.signal(s, h)
    if stopped:
        rc = stopped[0]
        if profile.executor.kind == "google-batch":
            time.sleep(float(env.get("NF_SWEEP_DELAY", "30")))
            try:
                from .batch import cancel_labelled

                cancel_labelled({"nf-session": session}, region=profile.executor.region, log=err)
            except Exception as e:  # noqa: BLE001 — the record must still finish
                err(f"scan head: batch-cancel failed: {type(e).__name__}: {e}")
        error = f"head terminated (signal; session {session})"
    else:
        f = first_failed(trace) if rc else None
        error = f"nextflow exit {rc}" + (f": {f} failed" if f else "") + f" (session {session})" if rc else None
    publish_reports(profile, scan, launch)
    if recording:
        _record(finish_cmd, finish_args(rc, error, cost, launch / "dag.mmd"))
    if rc == 0:
        print(f"SCAN-JOB-DONE {scan} -> {profile.data.rstrip('/')}/{profile.layout.snapshots_dir()}/{scan}")
    return rc
