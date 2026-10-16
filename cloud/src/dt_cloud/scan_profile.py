"""A deployment's scan profile (specs/pipelines-everywhere.md §3): what `dt-cloud scan head` runs and where, and what
each `dt-cloud scan step` reads. One YAML file per deployment, `job/profiles/<deployment>.yml`, on that deployment's
branch; generic code reads only its keys and holds no deployment's values (no buckets, projects, secrets, machines).

Validated on load: unknown keys are refused, and a missing required key is named by its path (`executor.project`).
Every problem is reported at once (`ProfileError`, one line each).

    deployment: <name>            # required: the session namespace (`disky/<deployment>/scan/<scan>`), alert titles
    executor:                     # required
      kind: google-batch | awsbatch | local
      project, region, service_account, image, boot_disk, spot     # google-batch (project, region, image required)
      queue, region, image, cli_path                               # awsbatch (queue, region, image required; unproven)
    work: gs://<bucket>/nf        # required: Nextflow's work dir (`<work>/work`) + resume cache (`<work>/cache`);
                                  # a local dir for `local`
    data: gs://<bucket>           # required: the store steps hand off through (FUSE-mounted at `$DT_DATA_MOUNT/<bucket>`
                                  # in a task); on google-batch it must be the work dir's bucket (the one Batch mounts)
    layout:                       # required
      layer2: <dir>/{scan}/       #   a scan's layer-2 dir (`LAYER2_PREFIX`): `<bucket>.parquet` + `index/<gen>/`
      snapshots: <subdir>         #   the site's snapshot subdir (`SNAPSHOTS_SUBDIR`; "" = `snapshots/`)
      listing: <dir>/{scan}/      #   where `list` writes a source's listing when `index` imports it (required then)
    sources:                      # required, ≥ 1: `list`'s fan-out
      - uri: s3://<bucket>        #   gs:// | gcs:// | s3:// | r2:// | hf://<namespace>
        machine: <type>           #   optional, else `machines.list`
        disk: 100 | {request: 375, type: local-ssd}
        procs: 8                  #   bulk-list `-P` (default: the task's cores)
        threads: 8                #   bulk-list `-w`
        endpoint: https://…       #   bulk-list `-E` (s3 / r2)
        region: <region>          #   reserved: nf-google 1.29.1 has no per-task location (refused unless = executor's)
        args: [-L, …]             #   extra lister args
    machines: {<label>: <type> | {type, disk, cpus, memory, time}}   # labels: small medium big list post (+ any)
    steps: [<extra step>, …]      # deployment-only steps on top of the core (`scan_steps.STEPS`, `core=False`)
    skip: [<core step>, …]        # core steps this deployment leaves out (each listed in `SKIPPABLE`)
    fold: [bucket-check, index-sync]   # tiny steps run inside a neighbour's task (default: both; [] = own tasks)
    secrets: {ENV: <secret>}      # Secret Manager names, fetched in-task (`dt-cloud secrets exec`)
    env: {KEY: value}             # every step's environment
    pass: [ENV, …]                # the head's own env vars passed through to every step when set (DT_RECORD, …)
    labels: {key: value}          # Batch labels on every task (+ `$DISKY_LABELS`, `component: scan`, `nf-session`)
    queue_size: 20                # Nextflow `executor.queueSize`
    options: {<step>: {…}}        # per-step settings, each step's keys in `scan_steps.STEPS[<step>].options`
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

EXECUTORS = ("google-batch", "awsbatch", "local")
#: A source URI's scheme → its kind (`disk-tree import -s`).
SOURCE_KINDS = {"gs": "gcs", "gcs": "gcs", "s3": "s3", "r2": "r2", "hf": "hf"}
#: Steps a task can absorb (spec §2 "Folding"): each runs in the named neighbour's task.
FOLDABLE = {"bucket-check": "list", "index-sync": "post"}
#: A local SSD's size on GCE: a `local-ssd` disk request is a multiple of it.
LOCAL_SSD_GB = 375

_EXECUTOR_KEYS = {
    "google-batch": ({"kind", "project", "region", "service_account", "image", "boot_disk", "spot"}, ("project", "region", "image")),
    "awsbatch": ({"kind", "queue", "region", "image", "cli_path"}, ("queue", "region", "image")),
    "local": ({"kind"}, ()),
}
_TOP = {"deployment", "executor", "work", "data", "layout", "sources", "machines", "steps", "skip", "fold", "secrets",
        "env", "pass", "labels", "queue_size", "options"}
_LAYOUT = {"layer2", "snapshots", "listing"}
_SOURCE = {"uri", "machine", "disk", "procs", "threads", "endpoint", "region", "args"}
_MACHINE = {"type", "disk", "cpus", "memory", "time"}
_DISK = {"request", "type"}
_DISK_TYPES = ("local-ssd", "pd-balanced", "pd-ssd", "pd-standard")
_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ProfileError(ValueError):
    """Every problem with a profile, one per line."""

    def __init__(self, problems: list[str], where: str = "profile"):
        self.problems = problems
        super().__init__(f"{where}: " + "; ".join(problems) if len(problems) == 1 else f"{where}:\n  " + "\n  ".join(problems))


@dataclass(frozen=True)
class Disk:
    """A task's disk: `request` GB, of `type` (None: the boot disk resized, pd-balanced)."""
    request: int
    type: str | None = None


@dataclass(frozen=True)
class Machine:
    type: str | None = None
    disk: Disk | None = None
    cpus: int | None = None
    memory: str | None = None
    time: str | None = None


@dataclass(frozen=True)
class Source:
    uri: str
    machine: str | None = None
    disk: Disk | None = None
    procs: int | None = None
    threads: int | None = None
    endpoint: str | None = None
    region: str | None = None
    args: tuple[str, ...] = ()

    @property
    def kind(self) -> str:
        return SOURCE_KINDS[urlparse(self.uri).scheme]

    @property
    def bucket(self) -> str:
        """The scan root's name: the bucket (or the hf namespace) — a layer-2's first path segment."""
        return urlparse(self.uri).netloc


@dataclass(frozen=True)
class Layout:
    layer2: str
    snapshots: str
    listing: str | None = None

    def snapshots_dir(self) -> str:
        return f"snapshots/{self.snapshots}".rstrip("/")


@dataclass(frozen=True)
class Executor:
    kind: str
    project: str | None = None
    region: str | None = None
    service_account: str | None = None
    image: str | None = None
    boot_disk: int | None = None
    spot: bool | None = None
    queue: str | None = None
    cli_path: str | None = None


@dataclass(frozen=True)
class ScanProfile:
    deployment: str
    executor: Executor
    work: str
    data: str
    layout: Layout
    sources: tuple[Source, ...]
    machines: Mapping[str, Machine] = field(default_factory=dict)
    steps: tuple[str, ...] = ()
    skip: tuple[str, ...] = ()
    fold: tuple[str, ...] = tuple(FOLDABLE)
    secrets: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    passthrough: tuple[str, ...] = ()
    labels: Mapping[str, str] = field(default_factory=dict)
    queue_size: int = 20
    options: Mapping[str, Mapping[str, object]] = field(default_factory=dict)

    def source(self, uri: str) -> Source:
        for s in self.sources:
            if s.uri == uri:
                return s
        raise KeyError(f"{uri!r} is not one of the profile's sources ({', '.join(s.uri for s in self.sources)})")

    def opts(self, step: str) -> Mapping[str, object]:
        return self.options.get(step, {})

    def enabled(self) -> tuple[str, ...]:
        """Every step this deployment runs, in `STEPS` order: the core minus `skip`, plus `steps`."""
        from .scan_steps import STEPS

        return tuple(n for n, s in STEPS.items() if (s.core and n not in self.skip) or n in self.steps)

    @property
    def data_bucket(self) -> str | None:
        u = urlparse(self.data)
        return u.netloc if u.scheme else None


def _bucket(uri: str) -> str | None:
    u = urlparse(uri)
    return u.netloc if u.scheme in ("gs", "s3", "r2") else None


def _int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _str_map(v: object, path: str, problems: list[str], key_re: re.Pattern | None = None) -> dict[str, str]:
    if v is None:
        return {}
    if not isinstance(v, Mapping):
        problems.append(f"{path}: a mapping")
        return {}
    out = {}
    for k, x in v.items():
        if key_re and not key_re.fullmatch(str(k)):
            problems.append(f"{path}.{k}: not an env var name")
        if isinstance(x, (Mapping, list)) or x is None:
            problems.append(f"{path}.{k}: a scalar")
            continue
        out[str(k)] = str(x).lower() if isinstance(x, bool) else str(x)
    return out


def _str_list(v: object, path: str, problems: list[str]) -> tuple[str, ...]:
    if v is None:
        return ()
    if not isinstance(v, list) or not all(isinstance(x, (str, int, float)) and not isinstance(x, bool) for x in v):
        problems.append(f"{path}: a list of strings")
        return ()
    return tuple(str(x) for x in v)


def _unknown(d: Mapping, known: set[str], path: str, problems: list[str]) -> None:
    for k in d:
        if k not in known:
            problems.append(f"{path}{k}: unknown key")


def _disk(v: object, path: str, problems: list[str]) -> Disk | None:
    """`100` (GB, the boot disk) or `{request: 1500, type: local-ssd}` (a local SSD's request a multiple of 375 GB; the
    counts a machine allows — n2-highmem-32: 0/4/8/16/24 — are GCE's to check, at submit)."""
    if v is None:
        return None
    if _int(v):
        if v <= 0:
            problems.append(f"{path}: a positive size in GB")
        return Disk(v)
    if not isinstance(v, Mapping):
        problems.append(f"{path}: a size in GB or {{request, type}}")
        return None
    _unknown(v, _DISK, f"{path}.", problems)
    req, typ = v.get("request"), v.get("type")
    if not _int(req) or req <= 0:
        problems.append(f"{path}.request: required, a positive size in GB")
        return None
    if typ is not None and typ not in _DISK_TYPES:
        problems.append(f"{path}.type: one of {', '.join(_DISK_TYPES)}")
    if typ == "local-ssd" and req % LOCAL_SSD_GB:
        problems.append(f"{path}.request: a multiple of {LOCAL_SSD_GB} for local-ssd (got {req})")
    return Disk(req, typ)


def parse(d: object, where: str = "profile") -> ScanProfile:
    """A validated `ScanProfile` from the YAML's mapping; `ProfileError` lists every problem."""
    from .scan_steps import SKIPPABLE, STEPS

    problems: list[str] = []
    if not isinstance(d, Mapping):
        raise ProfileError(["not a mapping"], where)
    _unknown(d, _TOP, "", problems)
    missing = [k for k in ("deployment", "executor", "work", "data", "layout", "sources") if d.get(k) in (None, "", [], {})]
    problems += [f"{k}: required" for k in missing]

    deployment = str(d.get("deployment") or "")
    if deployment and not _NAME.fullmatch(deployment):
        problems.append(f"deployment: lowercase letters, digits and '-' (got {deployment!r})")

    ex = d.get("executor") or {}
    executor = Executor(kind="local")
    if not isinstance(ex, Mapping):
        problems.append("executor: a mapping")
    elif ex:
        kind = ex.get("kind")
        if kind not in EXECUTORS:
            problems.append(f"executor.kind: required, one of {', '.join(EXECUTORS)}" + (f" (got {kind!r})" if kind else ""))
        else:
            known, required = _EXECUTOR_KEYS[kind]
            _unknown(ex, known, "executor.", problems)
            for k in required:
                if not ex.get(k):
                    problems.append(f"executor.{k}: required for {kind}")
            if ex.get("boot_disk") is not None and not _int(ex["boot_disk"]):
                problems.append("executor.boot_disk: a size in GB")
            executor = Executor(**{k: ex[k] for k in ex if k in known})

    work, data = str(d.get("work") or ""), str(d.get("data") or "")
    if executor.kind == "google-batch" and work and data:
        if not work.startswith("gs://"):
            problems.append("work: a gs:// URL for google-batch")
        elif _bucket(data) != _bucket(work):
            problems.append(f"data: must be in work's bucket ({_bucket(work)}): google-batch tasks mount only the work dir's bucket")
    if executor.kind == "local" and work and urlparse(work).scheme:
        problems.append("work: a local dir for the local executor (it refuses a cloud work dir without Fusion)")

    lay = d.get("layout") or {}
    layout = Layout("", "")
    if not isinstance(lay, Mapping):
        problems.append("layout: a mapping")
    elif lay:
        _unknown(lay, _LAYOUT, "layout.", problems)
        if not lay.get("layer2"):
            problems.append("layout.layer2: required")
        elif "{scan}" not in str(lay["layer2"]):
            problems.append("layout.layer2: a template with `{scan}`")
        if lay.get("snapshots") is None:
            problems.append("layout.snapshots: required (\"\" for `snapshots/` itself)")
        if lay.get("listing") is not None and "{scan}" not in str(lay["listing"]):
            problems.append("layout.listing: a template with `{scan}`")
        layout = Layout(str(lay.get("layer2") or ""), str(lay.get("snapshots") or ""),
                        str(lay["listing"]) if lay.get("listing") is not None else None)

    sources: list[Source] = []
    srcs = d.get("sources") or []
    if not isinstance(srcs, list):
        problems.append("sources: a list")
        srcs = []
    seen: set[str] = set()
    for i, s in enumerate(srcs):
        p = f"sources[{i}]"
        if isinstance(s, str):
            s = {"uri": s}
        if not isinstance(s, Mapping):
            problems.append(f"{p}: a URI or a mapping")
            continue
        _unknown(s, _SOURCE, f"{p}.", problems)
        uri = str(s.get("uri") or "")
        u = urlparse(uri)
        if not uri:
            problems.append(f"{p}.uri: required")
            continue
        if u.scheme not in SOURCE_KINDS or not u.netloc:
            problems.append(f"{p}.uri: <scheme>://<bucket> with scheme one of {', '.join(SOURCE_KINDS)} (got {uri!r})")
            continue
        if u.netloc in seen:
            problems.append(f"{p}.uri: bucket {u.netloc!r} listed twice")
        seen.add(u.netloc)
        for k in ("procs", "threads"):
            if s.get(k) is not None and (not _int(s[k]) or s[k] <= 0):
                problems.append(f"{p}.{k}: a positive integer")
        region = s.get("region")
        if region and region != executor.region:
            problems.append(f"{p}.region: {region!r} ≠ the executor's ({executor.region!r}); nf-google 1.29.1 has no "
                            f"per-task location (spec §4 'Per-source region')")
        sources.append(Source(uri, s.get("machine"), _disk(s.get("disk"), f"{p}.disk", problems), s.get("procs"),
                              s.get("threads"), s.get("endpoint"), region, _str_list(s.get("args"), f"{p}.args", problems)))

    machines: dict[str, Machine] = {}
    ms = d.get("machines") or {}
    if not isinstance(ms, Mapping):
        problems.append("machines: a mapping")
        ms = {}
    for label, m in ms.items():
        p = f"machines.{label}"
        if not _NAME.fullmatch(str(label)):
            problems.append(f"{p}: a label of lowercase letters, digits and '-'")
        if isinstance(m, str):
            machines[str(label)] = Machine(type=m)
            continue
        if not isinstance(m, Mapping):
            problems.append(f"{p}: a machine type or {{type, disk, cpus, memory, time}}")
            continue
        _unknown(m, _MACHINE, f"{p}.", problems)
        if m.get("cpus") is not None and not _int(m["cpus"]):
            problems.append(f"{p}.cpus: an integer")
        machines[str(label)] = Machine(m.get("type"), _disk(m.get("disk"), f"{p}.disk", problems), m.get("cpus"),
                                       m.get("memory"), m.get("time"))

    steps = _str_list(d.get("steps"), "steps", problems)
    for s in steps:
        if s not in STEPS:
            problems.append(f"steps: unknown step {s!r}")
        elif STEPS[s].core:
            problems.append(f"steps: {s!r} is a core step (always on unless in `skip`)")
    skip = _str_list(d.get("skip"), "skip", problems)
    for s in skip:
        if s not in SKIPPABLE:
            problems.append(f"skip: {s!r} can't be skipped (one of {', '.join(SKIPPABLE)})")
    fold = _str_list(d["fold"], "fold", problems) if "fold" in d else tuple(FOLDABLE)
    for s in fold:
        if s not in FOLDABLE:
            problems.append(f"fold: {s!r} can't be folded (one of {', '.join(FOLDABLE)})")

    options: dict[str, dict[str, object]] = {}
    opts = d.get("options") or {}
    if not isinstance(opts, Mapping):
        problems.append("options: a mapping")
        opts = {}
    for name, o in opts.items():
        if name not in STEPS:
            problems.append(f"options.{name}: unknown step")
            continue
        if not isinstance(o, Mapping):
            problems.append(f"options.{name}: a mapping")
            continue
        spec = STEPS[name].options
        for k, v in o.items():
            if k not in spec:
                problems.append(f"options.{name}.{k}: unknown key" + (f" (known: {', '.join(spec)})" if spec else " (the step takes none)"))
            elif not _typed(v, spec[k]):
                problems.append(f"options.{name}.{k}: {_TYPE_NAMES[spec[k]]}")
        options[name] = dict(o)

    qs = d.get("queue_size", 20)
    if not _int(qs) or qs <= 0:
        problems.append("queue_size: a positive integer")

    profile = ScanProfile(
        deployment=deployment, executor=executor, work=work, data=data, layout=layout, sources=tuple(sources),
        machines=machines, steps=steps, skip=skip, fold=fold,
        secrets=_str_map(d.get("secrets"), "secrets", problems, _ENV_NAME), env=_str_map(d.get("env"), "env", problems, _ENV_NAME),
        passthrough=_str_list(d.get("pass"), "pass", problems), labels=_str_map(d.get("labels"), "labels", problems),
        queue_size=qs if _int(qs) else 20, options=options,
    )
    if not missing:  # the steps' own checks, once the profile has its shape
        for name in profile.enabled():
            problems += STEPS[name].requires(profile)
    if problems:
        raise ProfileError(problems, where)
    return profile


_TYPE_NAMES = {str: "a string", int: "an integer", bool: "true or false", list: "a list of strings"}


def _typed(v: object, t: type) -> bool:
    if t is list:
        return isinstance(v, list) and all(isinstance(x, str) for x in v)
    if t is int:
        return _int(v)
    return isinstance(v, t)


def load(path: str | Path) -> ScanProfile:
    import yaml

    p = Path(path)
    return parse(yaml.safe_load(p.read_text()), where=str(p))


def to_json(path: str | Path) -> str:
    """The profile file as compact JSON (what the head hands each task: `DT_SCAN_PROFILE_JSON`), validated first."""
    import json

    import yaml

    p = Path(path)
    raw = yaml.safe_load(p.read_text())
    parse(raw, where=str(p))
    return json.dumps(raw, separators=(",", ":"), sort_keys=True)
