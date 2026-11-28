"""`scan-run rerun SCAN -s STEP`: a scan's failed soft steps re-run once their cause is fixed (specs/scan-step-rerun.md
phase 2). The scan's head job is submitted again (`rerun_spec`): its own spec, its command given `--only STEP`, and the
scan's identity (`SNAP_ID`, `GEN`) with the run it recovers (`$DT_RECOVERS`). That head records itself as a
`phase-recovery` child of the scan's run (`scan_head.head`), each re-run step's phase recorded into it as the steps
record theirs, and /scans reads a succeeded child as recovering its parent's failed steps."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from collections.abc import Mapping, Sequence
from copy import deepcopy

from click import argument, command, option

#: The run a head with `--only` re-runs steps of: set, the head records itself as that run's `phase-recovery` child.
RECOVERS_ENV = "DT_RECOVERS"


def err(*a) -> None:
    print(*a, file=sys.stderr)


def rerun_spec(job: Mapping, *, scan: str, gen: str, parent: str, steps: Sequence[str], by: str | None = None) -> dict:
    """The scan's head job (``job``, as Batch describes it) as a new job re-running ``steps`` of ``scan``: its one task's
    runnable and resources, its allocation policy, labels and logs policy (nothing Batch fills in: no name, uid, status or
    times), the head's command given `--only <step>` each, and in its env the scan's identity (`SNAP_ID`, `SCAN_ID`, `GEN`),
    the run it recovers (`$DT_RECOVERS` = ``parent``), `DT_TRIGGER=manual` and who (``by``: `$DT_TRIGGER_BY`). The head
    stamps its own job start and run id (`DT_RUN_ID` is dropped). Secrets stay the job's references; never a value."""
    groups = job["taskGroups"]
    if len(groups) != 1 or len(groups[0]["taskSpec"]["runnables"]) != 1:
        raise ValueError("the scan's head job has one task group of one runnable")
    src = groups[0]["taskSpec"]
    runnable = deepcopy(src["runnables"][0])
    container = runnable["container"]
    cmds = list(container.get("commands", []))
    at = next((i + 2 for i in range(len(cmds) - 1) if cmds[i:i + 2] == ["scan", "head"]), None)
    if container.get("entrypoint") != "dt-cloud" or at is None:
        raise ValueError(f"the scan's head job doesn't run `dt-cloud scan head`: {container.get('entrypoint')} {' '.join(cmds)}")
    container["commands"] = [*cmds[:at], *(a for s in steps for a in ("--only", s)), *cmds[at:]]
    environment = deepcopy(src.get("environment", {}))
    env = environment.setdefault("variables", {})
    for k in ("DT_RUN_ID", "DT_TRIGGER_BY", "DT_JOB_START"):
        env.pop(k, None)
    env.update({"SNAP_ID": scan, "SCAN_ID": scan, "GEN": gen, "DT_TRIGGER": "manual", RECOVERS_ENV: parent,
                **({"DT_TRIGGER_BY": by} if by else {})})
    task = {k: deepcopy(v) for k, v in src.items() if k not in ("runnables", "environment")}
    task.update(runnables=[runnable], environment=environment, maxRetryCount=0)
    return {"taskGroups": [{"taskCount": 1, "taskSpec": task}], "allocationPolicy": deepcopy(job["allocationPolicy"]),
            "labels": deepcopy(job.get("labels", {})), "logsPolicy": deepcopy(job.get("logsPolicy") or {"destination": "CLOUD_LOGGING"})}


def rerun_job_id(head_job: str, now: dt.datetime) -> str:
    """`<the head job's name>-rerun-<HHMMSS>`, within Batch's 63 chars."""
    tail = f"-rerun-{now:%H%M%S}"
    return head_job[:63 - len(tail)] + tail


def rerun_target(sink, store: str, scan: str, steps: Sequence[str]) -> dict:
    """The scan's run (its newest `scan` run in ``store``) whose failed phases ``steps`` re-run: refused while it or an
    earlier re-run of it is running, or when a step isn't among its failed phases."""
    from .scan_runs import lit, phase_status, stored

    rows = sink.query(f"SELECT run_id, status, gen, job_name, region FROM scan_runs WHERE store = {lit(store)} "
                      f"AND scan = {lit(scan)} AND kind = 'scan' ORDER BY started_ts DESC LIMIT 1")
    if not rows:
        raise SystemExit(f"scan-run rerun: no run of scan {scan} in store {store}")
    run = rows[0]
    if run["status"] == "running":
        raise SystemExit(f"scan-run rerun: {run['run_id']} is still running")
    live = sink.query(f"SELECT run_id FROM scan_runs WHERE parent = {lit(run['run_id'])} AND kind = 'phase-recovery' AND status = 'running'")
    if live:
        raise SystemExit(f"scan-run rerun: {run['run_id']} has a re-run running ({live[0]['run_id']})")
    _, phases = stored(sink, run["run_id"])
    failed = {p["phase"] for p in phases if p["status"] == "failed" or phase_status(p.get("note")) == "failed"}
    if missing := [s for s in steps if s not in failed]:
        raise SystemExit(f"scan-run rerun: {run['run_id']} has no failed phase {', '.join(missing)} (failed: {', '.join(sorted(failed)) or 'none'})")
    return run


@command("rerun")
@option("-b", "--by", default=None, help="Who starts it ($DT_TRIGGER_BY, default its env value): an address or handle, recorded as the re-run's trigger")
@option("-c", "--profile", "profile_path", default=None, help="Deployment profile (default $SCAN_RUNS_PROFILE)")
@option("-d", "--d1", default=None, help="D1 database id (default $D1_DB_ID)")
@option("-n", "--dry-run", is_flag=True, help="Print the job's spec; submit nothing")
@option("-s", "--step", "steps", multiple=True, required=True, help="A failed soft step of the scan to re-run (repeatable; `scan steps` lists them)")
@argument("scan")
def rerun_cmd(by: str | None, profile_path: str | None, d1: str | None, dry_run: bool, steps: tuple[str, ...], scan: str) -> None:
    """Re-run SCAN's failed soft steps (specs/scan-step-rerun.md): its head job again, on the deployment's live image, with
    `--only` each step, recorded as a `phase-recovery` child of the scan's run (/scans reads a succeeded one as
    recovering it). A core step isn't re-run alone: a scan whose core step failed is resumed whole. Prints the job."""
    from .batch import submit_job
    from .gcp import batch_job
    from .scan_runs import load_profile, sink_for
    from .scan_steps import STEPS

    if bad := [s for s in steps if s not in STEPS]:
        raise SystemExit(f"scan-run rerun: no step {', '.join(bad)}")
    if core := [s for s in steps if not STEPS[s].soft]:
        raise SystemExit(f"scan-run rerun: {', '.join(core)} {'is a core step' if len(core) == 1 else 'are core steps'}: "
                         "resume the scan's whole head instead")
    profile = load_profile(profile_path)
    run = rerun_target(sink_for(False, None, d1), profile.store, scan, steps)
    job = batch_job(run["job_name"], profile.project, run["region"])
    spec = rerun_spec(job, scan=scan, gen=run["gen"], parent=run["run_id"], steps=steps, by=by or os.environ.get("DT_TRIGGER_BY"))
    job_id = rerun_job_id(run["job_name"], dt.datetime.now(dt.timezone.utc))
    if dry_run:
        print(json.dumps(spec, indent=1))
        err(f"scan-run rerun: would submit {job_id} in {run['region']}: {', '.join(steps)} of {scan} (recovering {run['run_id']})")
        return
    print(submit_job(spec, job_id, region=run["region"], project=profile.project))
    err(f"scan-run rerun: {', '.join(steps)} of {scan}, recovering {run['run_id']}")
