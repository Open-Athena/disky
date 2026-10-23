"""`dt-cloud cost`: what a deployment's GCP storage and Batch compute cost now, and projected over horizons.

Read-only, from measurements:
1. Storage: Cloud Monitoring's `storage.googleapis.com/storage/v2/total_bytes` per bucket, daily means over the
   window, split by its `type` (live vs soft-deleted objects) and `storage_class` labels. Live bytes grow at their
   least-squares slope; soft-deleted bytes track churn, not growth, so they are held at their current level (or at
   zero for a bucket whose soft-delete retention is off: they drain).
2. Compute: the Batch jobs (every location, or `-L`) created in the window whose labels match a selector. A job's
   VM-hours are its `runDuration` × min(`taskCount`, `parallelism`) (its first task group), priced by its machine
   type and provisioning model (SPOT × `spot_factor`). The window's $ / its days is the rate, projected flat.

Prices are approximate US list prices (`PRICES`), overridable by a YAML file (`load_prices`): estimates, not
invoices; no discounts, disks, network or operations. A machine type or storage class with no price is reported
as unpriced, never dropped.

The fetchers (`fetch_series`, `fetch_jobs`, `select_buckets`, `soft_delete_off`) are the only network calls; the
rest takes the APIs' JSON as given, so tests inject fake series and jobs.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from click import UsageError, command, option

from .scan_cost import labels_of, timestamp

METRIC = "storage.googleapis.com/storage/v2/total_bytes"
LIVE = "live-object"
DAY = 86400
GB = 1e9
GIB = 2**30
YEAR_DAYS = 365.25
MONTH_DAYS = YEAR_DAYS / 12

#: $/vCPU-hour and $/GB-hour (on-demand, US regions) per machine family; a predefined type's price is their sum
#: over its vCPUs and memory, which is how GCP prices them.
FAMILY_RATES = {
    "e2": (0.021811, 0.002923),
    "n2": (0.031611, 0.004237),
    "n2d": (0.027502, 0.003686),
}
#: GB of memory per vCPU by shape.
SHAPES = {"standard": 4, "highmem": 8}
SIZES = {"e2": (2, 4, 8, 16, 32), "n2": (2, 4, 8, 16, 32, 48, 64, 80, 96), "n2d": (2, 4, 8, 16, 32, 48, 64, 80, 96)}


def _machine_prices() -> dict[str, float]:
    out = {}
    for family, (cpu, mem) in FAMILY_RATES.items():
        for shape, gb_per_cpu in SHAPES.items():
            for n in SIZES[family]:
                out[f"{family}-{shape}-{n}"] = round(n * cpu + n * gb_per_cpu * mem, 6)
    # Shared-core E2s are priced per type, not per resource.
    out |= {"e2-micro": 0.008376, "e2-small": 0.016751, "e2-medium": 0.033503}
    return out


@dataclass(frozen=True)
class Prices:
    """$/GiB-month per Monitoring `storage_class` label value (a regional bucket's Standard storage is `REGIONAL`,
    a multi-region's `MULTI_REGIONAL`), on-demand $/hour per machine type, and the SPOT multiplier."""

    storage: Mapping[str, float]
    machines: Mapping[str, float]
    spot_factor: float


#: Approximate US list prices (USD). Regional buckets in other continents and Spot's per-region discounts differ:
#: override with `-P` (`load_prices`).
PRICES = Prices(
    storage={
        "REGIONAL": 0.020,
        "STANDARD": 0.020,
        "DURABLE_REDUCED_AVAILABILITY": 0.020,
        "MULTI_REGIONAL": 0.026,
        "NEARLINE": 0.010,
        "COLDLINE": 0.004,
        "ARCHIVE": 0.0012,
    },
    machines=_machine_prices(),
    spot_factor=0.3,
)


def load_prices(path: Path | None, base: Prices = PRICES) -> Prices:
    """`base`, with `path`'s (YAML) `storage` / `machines` entries added or replacing, and its `spot_factor`."""
    if path is None:
        return base
    doc = yaml.safe_load(path.read_text()) or {}
    unknown = set(doc) - {"storage", "machines", "spot_factor"}
    if unknown:
        raise UsageError(f"{path}: unknown keys {sorted(unknown)} (expected storage, machines, spot_factor)")
    return Prices(
        storage={**base.storage, **{k: float(v) for k, v in (doc.get("storage") or {}).items()}},
        machines={**base.machines, **{k: float(v) for k, v in (doc.get("machines") or {}).items()}},
        spot_factor=float(doc.get("spot_factor", base.spot_factor)),
    )


# --- parsing ------------------------------------------------------------------------------------------------

def parse_selector(text: str | None) -> dict[str, str]:
    """`"k=v,k2=v2"` → `{"k": "v", "k2": "v2"}`; None / empty → `{}`. A value may list alternatives, `v1|v2`
    (`matches`): e.g. a deployment renamed within the window."""
    out: dict[str, str] = {}
    for item in (text or "").split(","):
        if not item.strip():
            continue
        k, sep, v = item.strip().partition("=")
        if not sep or not k:
            raise UsageError(f"label selector items are KEY=VALUE: {item!r}")
        out[k] = v
    return out


def matches(labels: Mapping[str, str], selector: Mapping[str, str]) -> bool:
    return all(labels.get(k) in v.split("|") for k, v in selector.items())


SPAN = re.compile(r"(\d+(?:\.\d+)?)([dwmy])")
SPAN_DAYS = {"d": 1.0, "w": 7.0, "m": MONTH_DAYS, "y": YEAR_DAYS}


def parse_span(text: str) -> float:
    """`30d`, `2w`, `6m`, `1y` → days."""
    m = SPAN.fullmatch(text.strip())
    if not m:
        raise UsageError(f"spans are a number and d|w|m|y: {text!r}")
    return float(m[1]) * SPAN_DAYS[m[2]]


@dataclass(frozen=True)
class Series:
    """One bucket's bytes of one `type` and `storage_class`: (epoch seconds, bytes), oldest first."""

    bucket: str
    type: str
    storage_class: str
    points: tuple[tuple[float, float], ...]


def parse_series(pages: Iterable[Mapping]) -> list[Series]:
    """Monitoring `timeSeries.list` pages → `Series`, sorted by (bucket, type, class)."""
    out = []
    for page in pages:
        for ts in page.get("timeSeries", []):
            pts = sorted(
                (timestamp(p["interval"]["endTime"]), float(p["value"].get("doubleValue", p["value"].get("int64Value", 0))))
                for p in ts.get("points", [])
            )
            m = ts["metric"]["labels"]
            out.append(Series(ts["resource"]["labels"]["bucket_name"], m["type"], m["storage_class"], tuple(pts)))
    return sorted(out, key=lambda s: (s.bucket, s.type, s.storage_class))


def slope_per_day(points: Sequence[tuple[float, float]]) -> float:
    """Least-squares slope of bytes over days; 0 with fewer than two points."""
    if len(points) < 2:
        return 0.0
    xs = [t / DAY for t, _ in points]
    ys = [v for _, v in points]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0


@dataclass(frozen=True)
class JobUsage:
    name: str
    location: str
    component: str
    machine: str | None
    spot: bool
    vm_hours: float


def machine_key(u: JobUsage) -> str:
    return f"{u.machine or '(unknown)'}{' spot' if u.spot else ''}"


def machine_of(job: Mapping) -> tuple[str | None, bool]:
    """The job's machine type and whether it is SPOT: `allocationPolicy.instances[0].policy`, else (a template-based
    job) the instances its status reports; no `provisioningModel` = STANDARD."""
    ins = job.get("allocationPolicy", {}).get("instances", [])
    policy = ins[0].get("policy", {}) if ins else {}
    machine = policy.get("machineType")
    model = policy.get("provisioningModel")
    if machine is None:
        for group in job.get("status", {}).get("taskGroups", {}).values():
            for i in group.get("instances", []):
                machine, model = i.get("machineType"), model or i.get("provisioningModel")
                break
    return machine, model == "SPOT"


def job_usage(job: Mapping) -> JobUsage:
    group = job.get("taskGroups", [{}])[0]
    tasks = int(group.get("taskCount", 1))
    vms = min(tasks, int(group.get("parallelism", tasks)))
    seconds = float(job.get("status", {}).get("runDuration", "0s").rstrip("s"))
    machine, spot = machine_of(job)
    return JobUsage(
        name=job["name"].rsplit("/", 1)[-1],
        location=job["name"].split("/")[3],
        component=labels_of(job).get("component", "(none)"),
        machine=machine,
        spot=spot,
        vm_hours=seconds * vms / 3600,
    )


# --- the report ---------------------------------------------------------------------------------------------

def storage_report(
    series: Sequence[Series],
    prices: Prices,
    horizons: Mapping[str, float],
    drains: Iterable[str] = (),
) -> dict:
    """Per (bucket, type, class): current bytes (the newest daily mean), live bytes' slope, $/mo; totals now and at
    each horizon (live bytes extrapolated, floored at 0; soft-deleted held, or 0 in a `drains` bucket)."""
    drained = set(drains)
    rows: list[dict[str, Any]] = []
    unpriced: dict[str, float] = defaultdict(float)
    at: dict[str, dict[str, float]] = {h: {"bytes": 0.0, "usd_per_month": 0.0} for h in horizons}
    for s in series:
        if not s.points:
            continue
        current = s.points[-1][1]
        live = s.type == LIVE
        slope = slope_per_day(s.points) if live else 0.0
        price = prices.storage.get(s.storage_class)
        if price is None:
            unpriced[s.storage_class] += current
        rows.append({
            "bucket": s.bucket,
            "type": s.type,
            "storage_class": s.storage_class,
            "days": len(s.points),
            "bytes": current,
            "slope_bytes_per_day": slope,
            "usd_per_month": None if price is None else current / GIB * price,
        })
        for h, days in horizons.items():
            if live:
                b = max(0.0, current + slope * days)
            else:
                b = 0.0 if s.bucket in drained else current
            at[h]["bytes"] += b
            if price is not None:
                at[h]["usd_per_month"] += b / GIB * price
    by_type = {t: sum(r["bytes"] for r in rows if r["type"] == t) for t in sorted({r["type"] for r in rows})}
    return {
        "rows": rows,
        "bytes": sum(r["bytes"] for r in rows),
        "bytes_by_type": by_type,
        "live_slope_bytes_per_day": sum(r["slope_bytes_per_day"] for r in rows),
        "usd_per_month": sum(r["usd_per_month"] or 0.0 for r in rows),
        "drains": sorted(drained & {r["bucket"] for r in rows}),
        "unpriced_bytes": dict(sorted(unpriced.items())),
        "horizons": at,
    }


def job_usd(u: JobUsage, prices: Prices) -> float | None:
    """`u`'s VM-hours at its machine's price (× `spot_factor` on SPOT); None when the machine has no price."""
    rate = prices.machines.get(u.machine) if u.machine else None
    return None if rate is None else u.vm_hours * rate * (prices.spot_factor if u.spot else 1.0)


def _tally(rows: Iterable[tuple[str, JobUsage, float | None]], window_days: float) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = defaultdict(lambda: {"jobs": 0, "vm_hours": 0.0, "usd": 0.0})
    for k, u, usd in rows:
        out[k]["jobs"] += 1
        out[k]["vm_hours"] += u.vm_hours
        out[k]["usd"] += usd or 0.0
    return {k: {**v, "usd_per_month": v["usd"] / window_days * MONTH_DAYS} for k, v in sorted(out.items())}


def compute_report(
    jobs: Sequence[Mapping],
    selector: Mapping[str, str],
    prices: Prices,
    window_days: float,
) -> dict:
    """The selector's jobs' VM-hours and $ per component and machine, as $/day and $/mo over the window; unpriced
    machines' VM-hours; and the window's jobs that carry none of the selector's keys, by name prefix (work the
    selector can't see, which may or may not be the deployment's)."""
    priced = [(u, job_usd(u, prices)) for u in (job_usage(j) for j in jobs if matches(labels_of(j), selector))]
    unpriced: dict[str, float] = defaultdict(float)
    for u, usd in priced:
        if usd is None:
            unpriced[machine_key(u)] += u.vm_hours
    unlabeled = [job_usage(j) for j in jobs if selector and not set(selector) & set(labels_of(j))]
    usd = sum(x or 0.0 for _, x in priced)
    per_day = usd / window_days
    return {
        "selector": dict(selector),
        "jobs": len(priced),
        "vm_hours": sum(u.vm_hours for u, _ in priced),
        "usd": usd,
        "usd_per_day": per_day,
        "usd_per_month": per_day * MONTH_DAYS,
        "components": _tally(((u.component, u, x) for u, x in priced), window_days),
        "machines": _tally(((machine_key(u), u, x) for u, x in priced), window_days),
        "unpriced_vm_hours": dict(sorted(unpriced.items())),
        "unlabeled": _tally(((u.name.split("-")[0], u, job_usd(u, prices)) for u in unlabeled), window_days),
    }


def report(
    *,
    name: str,
    project: str,
    now: float,
    window_days: float,
    horizons: Mapping[str, float],
    prices: Prices,
    series: Sequence[Series] | None,
    drains: Iterable[str] = (),
    jobs: Sequence[Mapping] | None = None,
    selector: Mapping[str, str] | None = None,
) -> dict:
    doc: dict = {
        "name": name,
        "project": project,
        "as_of": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": window_days,
        "horizons": dict(horizons),
        "basis": "approximate US list prices; estimates, not billed cost",
        "storage": None if series is None else storage_report(series, prices, horizons, drains),
        "compute": None if jobs is None else compute_report(jobs, selector or {}, prices, window_days),
    }
    doc["summary"] = summary(doc)
    return doc


def _gb(b: float) -> str:
    return f"{b / GB:,.1f}"


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def summary(doc: Mapping) -> str:
    """"We can leave NAME running for 1y/2y for X→Y GB of storage ($A→$B/mo) and $C→$D/mo on compute.\""""
    hs = list(doc["horizons"])
    parts = []
    if (s := doc["storage"]) is not None:
        ys = "/".join(_gb(s["horizons"][h]["bytes"]) for h in hs)
        bs = "/".join(_usd(s["horizons"][h]["usd_per_month"]) for h in hs)
        parts.append(f"{_gb(s['bytes'])}→{ys} GB of storage ({_usd(s['usd_per_month'])}→{bs}/mo)")
    if (c := doc["compute"]) is not None:
        parts.append(f"{_usd(c['usd_per_month'])}→{_usd(c['usd_per_month'])}/mo on compute")
    return f"We can leave {doc['name']} running for {'/'.join(hs)} for {' and '.join(parts)} (list-price estimates)."


def render(doc: Mapping) -> str:
    hs = list(doc["horizons"])
    lines = [
        f"{doc['name']} · {doc['project']} · {doc['window_days']:g}-day window to {doc['as_of']} · {doc['basis']}",
    ]
    if (s := doc["storage"]) is not None:
        lines += ["", "Storage (Cloud Monitoring `storage/v2/total_bytes`, daily means):",
                  f"  {'bucket':<32} {'type':<20} {'class':<15} {'days':>4} {'GB':>12} {'GB/day':>9} {'$/mo':>10}"]
        for r in s["rows"]:
            usd = "unpriced" if r["usd_per_month"] is None else _usd(r["usd_per_month"])
            slope = f"{r['slope_bytes_per_day'] / GB:+,.2f}" if r["type"] == LIVE else "-"
            lines.append(f"  {r['bucket']:<32} {r['type']:<20} {r['storage_class']:<15} {r['days']:>4} {_gb(r['bytes']):>12} {slope:>9} {usd:>10}")
        by_type = ", ".join(f"{t} {_gb(b)} GB" for t, b in s["bytes_by_type"].items())
        lines.append(f"  now: {_gb(s['bytes'])} GB ({by_type}), {_usd(s['usd_per_month'])}/mo; live {s['live_slope_bytes_per_day'] / GB:+,.2f} GB/day")
        for h in hs:
            at = s["horizons"][h]
            lines.append(f"  {h}: {_gb(at['bytes'])} GB, {_usd(at['usd_per_month'])}/mo")
        held = "soft-deleted bytes held at their current level"
        if s["drains"]:
            held += f", except {', '.join(s['drains'])} (soft delete off: they drain)"
        lines.append(f"  ({held})")
        for cls, b in s["unpriced_bytes"].items():
            lines.append(f"  UNPRICED: {_gb(b)} GB of storage class {cls}")
    if (c := doc["compute"]) is not None:
        sel = ",".join(f"{k}={v}" for k, v in c["selector"].items()) or "(any)"
        lines += ["", f"Compute (Batch jobs labeled {sel}, created in the window; VM-h = runDuration × min(taskCount, parallelism)):",
                  f"  {'component':<24} {'jobs':>5} {'VM-h':>9} {'$/mo':>10}"]
        for k, v in c["components"].items():
            lines.append(f"  {k:<24} {v['jobs']:>5} {v['vm_hours']:>9,.1f} {_usd(v['usd_per_month']):>10}")
        lines.append(f"  {'machine':<24} {'jobs':>5} {'VM-h':>9} {'$/mo':>10}")
        for k, v in c["machines"].items():
            lines.append(f"  {k:<24} {v['jobs']:>5} {v['vm_hours']:>9,.1f} {_usd(v['usd_per_month']):>10}")
        lines.append(f"  total: {c['jobs']} jobs, {c['vm_hours']:,.1f} VM-h, {_usd(c['usd'])} = {_usd(c['usd_per_day'])}/day = {_usd(c['usd_per_month'])}/mo (flat)")
        for k, h in c["unpriced_vm_hours"].items():
            lines.append(f"  UNPRICED: {h:,.1f} VM-h on {k}")
        if un := c["unlabeled"]:
            n = sum(v["jobs"] for v in un.values())
            h = sum(v["vm_hours"] for v in un.values())
            m = sum(v["usd_per_month"] for v in un.values())
            lines.append(f"  NOT COUNTED: {n} jobs in the window ({h:,.1f} VM-h, {_usd(m)}/mo) carry none of the selector's keys; by name prefix:")
            for k, v in sorted(un.items(), key=lambda kv: (-kv[1]["jobs"], kv[0])):
                lines.append(f"    {k:<22} {v['jobs']:>5} {v['vm_hours']:>9,.1f} {_usd(v['usd_per_month']):>10}")
    lines += ["", doc["summary"]]
    return "\n".join(lines) + "\n"


# --- fetching (read-only) -----------------------------------------------------------------------------------

def clock() -> float:
    return datetime.now(timezone.utc).timestamp()


def _rfc3339(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def select_buckets(project: str, selector: Mapping[str, str]) -> list[str]:
    """The project's buckets whose labels match `selector`."""
    from google.cloud.storage import Client

    return sorted(b.name for b in Client(project=project).list_buckets() if matches(b.labels or {}, selector))


def soft_delete_off(project: str, buckets: Sequence[str]) -> list[str]:
    """Those of `buckets` whose soft-delete retention is 0 (off)."""
    from google.cloud.storage import Client

    client = Client(project=project)
    out = []
    for name in buckets:
        policy = client.get_bucket(name).soft_delete_policy
        if not policy.retention_duration_seconds:
            out.append(name)
    return out


def fetch_series(project: str, buckets: Sequence[str], start: float, end: float) -> list[dict]:
    """`METRIC` for `buckets`, ALIGN_MEAN per day over [start, end]: the raw `timeSeries.list` pages."""
    from .gcp import pages

    names = ", ".join(json.dumps(b) for b in buckets)
    return pages(
        f"https://monitoring.googleapis.com/v3/projects/{project}/timeSeries",
        filter=f'metric.type="{METRIC}" resource.labels.bucket_name=one_of({names})',
        **{
            "interval.startTime": _rfc3339(start),
            "interval.endTime": _rfc3339(end),
            "aggregation.alignmentPeriod": f"{DAY}s",
            "aggregation.perSeriesAligner": "ALIGN_MEAN",
        },
    )


def fetch_jobs(project: str, locations: Sequence[str], since: float) -> list[dict]:
    """The Batch jobs created since `since`, in `locations` (empty: every location, `-`)."""
    from .gcp import pages

    out = []
    for loc in locations or ["-"]:
        for page in pages(
            f"https://batch.googleapis.com/v1/projects/{project}/locations/{loc}/jobs",
            filter=f'create_time>"{_rfc3339(since)}"',
            pageSize="1000",
        ):
            out.extend(page.get("jobs", []))
    return sorted(out, key=lambda j: j["name"])


@command("cost")
@option("-b", "--bucket", "buckets", multiple=True, help="A bucket to measure (repeatable)")
@option("-H", "--horizons", default="1y,2y", help="Projection horizons, comma-separated (`6m`, `1y`, `90d`, …)")
@option("-j", "--json", "as_json", is_flag=True, help="The report as JSON")
@option("-J", "--job-labels", default=None, help="Batch job label selector, `k=v,k=v`, a value may be `v1|v2` (e.g. `app=disky,deployment=NAME`); unset: no compute section")
@option("-l", "--bucket-labels", default=None, help="Bucket label selector, `k=v,k=v` (a value may be `v1|v2`): the project's buckets whose labels match (added to `-b`)")
@option("-L", "--location", "locations", multiple=True, help="Batch location (repeatable; default every location)")
@option("-n", "--name", default=None, help="What the summary calls the deployment (default: the selectors)")
@option("-p", "--project", default=None, help="GCP project (default `$GCP_PROJECT`, else ADC's)")
@option("-P", "--prices", "prices_path", type=Path, default=None, help="YAML overriding the built-in list prices: `storage` ($/GiB-month by class), `machines` ($/h by type), `spot_factor`")
@option("-w", "--window", default="30d", help="Measurement window (`30d`, `2w`, …)")
def cost_cmd(
    buckets: tuple[str, ...],
    horizons: str,
    as_json: bool,
    job_labels: str | None,
    bucket_labels: str | None,
    locations: tuple[str, ...],
    name: str | None,
    project: str | None,
    prices_path: Path | None,
    window: str,
) -> None:
    """A deployment's storage and compute cost now and projected over `-H` horizons, from the window's GCS
    Monitoring bytes and Batch jobs: list-price estimates. Read-only."""
    from .gcp import gcp_project

    if not (buckets or bucket_labels or job_labels):
        raise UsageError("nothing to measure: pass -b / -l (storage) and/or -J (compute)")
    project = project or gcp_project()
    prices = load_prices(prices_path)
    window_days = parse_span(window)
    spans = {h.strip(): parse_span(h) for h in horizons.split(",") if h.strip()}
    now = clock()
    start = now - window_days * DAY
    series = drains = None
    if buckets or bucket_labels:
        names = sorted({*buckets, *(select_buckets(project, parse_selector(bucket_labels)) if bucket_labels else [])})
        if not names:
            raise UsageError(f"no bucket in {project} matches {bucket_labels}")
        series = parse_series(fetch_series(project, names, start, now))
        drains = soft_delete_off(project, names)
    selector = parse_selector(job_labels)
    jobs = fetch_jobs(project, locations, start) if job_labels else None
    label = name or " / ".join(x for x in (job_labels, bucket_labels) if x) or ", ".join(buckets)
    doc = report(
        name=label, project=project, now=now, window_days=window_days, horizons=spans, prices=prices,
        series=series, drains=drains or (), jobs=jobs, selector=selector,
    )
    print(json.dumps(doc, indent=2) if as_json else render(doc), end="\n" if as_json else "")

